"""Read-only, source-linked acceptance probes for an owned prototype publication."""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from collections import defaultdict
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.contracts.evidence import EvidenceSpanV1_1
from fabric_kg_builder.contracts.receipts import ArtifactManifest
from fabric_kg_builder.deploy.schema2_prototype import (
    API, FORMAT_VERSION, MAX_GRAPH_READBACK_ROWS, PrototypePublicationError,
    _Run, _atomic_json, _compile, _compiler_hash, _definition_payloads,
    _graph_readback_checks, _graph_row_fingerprint, _native_definitions,
    _quote_graph_identifier, _read_json, _table_proof,
)
from fabric_kg_builder.enrichment.schema2_validation_stage import L3_EXTRACTION_PURPOSE, _safe_id
from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
from fabric_kg_builder.serving.evidence_retrieval import _validate_evidence_partitions
from fabric_kg_builder.serving.graph_model import _GMResponse, execute_gql_query

_TOKEN = re.compile(
    r"\s*(?:(?P<string>'(?:[^']|'')*')|(?P<quoted>`[A-Za-z0-9_]+`)"
    r"|(?P<number>\d+(?:\.\d+)?)|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<symbol>->|<-|<=|>=|<>|!=|[()\[\],.:=<>+-]))"
)


def _strict_json(path: Path) -> Any:
    def duplicate_guard(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PrototypePublicationError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def nonfinite(value: str) -> None:
        raise PrototypePublicationError(f"Nonfinite JSON value: {value}")

    return json.loads(
        path.read_text("utf-8"), object_pairs_hook=duplicate_guard,
        parse_constant=nonfinite,
    )


class _Query:
    """A deliberately small grammar, not a keyword blacklist for arbitrary GQL."""

    def __init__(self, text: str, schema: dict[str, Any], max_rows: int) -> None:
        if not isinstance(text, str) or len(text) > 16000:
            raise PrototypePublicationError("Query must be text of at most 16000 characters")
        self.tokens: list[tuple[str, str]] = []
        position = 0
        while position < len(text.rstrip()):
            match = _TOKEN.match(text, position)
            if match is None:
                raise PrototypePublicationError("Query contains unsupported syntax, comments, or statements")
            self.tokens.append((match.lastgroup, match.group(match.lastgroup)))
            position = match.end()
        self.index = 0
        self.schema = schema
        self.nodes: dict[str, str] = {}
        self.edges: dict[str, tuple[str, str, str]] = {}
        self.links: set[tuple[str, str]] = set()
        self.outputs: dict[str, tuple[str, str]] = {}
        self.identity_columns: dict[str, str] = {}
        self.max_rows = max_rows
        self.gql = self.parse()

    def peek(self, value: str) -> bool:
        return self.index < len(self.tokens) and self.tokens[self.index][1].upper() == value

    def take(self, value: str) -> None:
        if not self.peek(value):
            raise PrototypePublicationError(f"Expected {value} in bounded MATCH/RETURN query")
        self.index += 1

    def identifier(self) -> str:
        if self.index >= len(self.tokens) or self.tokens[self.index][0] not in ("word", "quoted"):
            raise PrototypePublicationError("Expected a schema identifier")
        value = self.tokens[self.index][1].strip("`")
        self.index += 1
        _quote_graph_identifier(value)
        return value

    def node(self) -> tuple[str, str]:
        self.take("(")
        variable = self.identifier()
        if self.peek(":"):
            self.take(":")
            label = self.identifier()
            if label not in self.schema["nodes"]:
                raise PrototypePublicationError(f"Unknown node label: {label}")
            if variable in self.nodes and self.nodes[variable] != label:
                raise PrototypePublicationError("A node variable cannot change semantic type")
            if variable in self.edges:
                raise PrototypePublicationError("Node/edge variable collision")
            self.nodes[variable] = label
        elif variable not in self.nodes:
            raise PrototypePublicationError("Every new node variable requires an approved label")
        self.take(")")
        return variable, f"({_quote_graph_identifier(variable)}:{_quote_graph_identifier(self.nodes[variable])})"

    def property(self) -> tuple[str, str, str]:
        variable = self.identifier()
        self.take(".")
        name = self.identifier()
        if variable in self.nodes:
            properties = self.schema["nodes"][self.nodes[variable]]["properties"]
        elif variable in self.edges:
            properties = self.schema["edges"][self.edges[variable][0]]["properties"]
        else:
            raise PrototypePublicationError(f"Unknown query variable: {variable}")
        if name not in properties:
            raise PrototypePublicationError(f"Unknown property: {variable}.{name}")
        return variable, name, f"{_quote_graph_identifier(variable)}.{_quote_graph_identifier(name)}"

    def literal(self) -> str:
        negative = self.peek("-")
        if negative:
            self.take("-")
        if self.index >= len(self.tokens):
            raise PrototypePublicationError("Missing predicate literal")
        kind, token = self.tokens[self.index]
        self.index += 1
        if kind == "number":
            return ("-" if negative else "") + token
        if kind == "string" and (
            "\\" in token or "'" in token[1:-1] or any(ord(character) < 32 for character in token)
        ):
            raise PrototypePublicationError("Escaped/control-character string literals are unsupported; filter by canonical ID")
        if not negative and (kind == "string" or token.upper() in ("TRUE", "FALSE")):
            return token
        raise PrototypePublicationError("Predicates accept only strings, numbers, booleans, or IS NULL")

    def parse(self) -> str:
        self.take("MATCH")
        paths = []
        hop_count = 0
        while True:
            current, path = self.node()
            while self.peek("-") or self.peek("<-"):
                reverse = self.peek("<-")
                self.take("<-" if reverse else "-")
                self.take("[")
                edge_variable = self.identifier()
                self.take(":")
                label = self.identifier()
                self.take("]")
                self.take("-" if reverse else "->")
                following, node_text = self.node()
                source, target = (following, current) if reverse else (current, following)
                edge = self.schema["edges"].get(label)
                if (
                    edge is None or edge["source_label"] != self.nodes[source]
                    or edge["target_label"] != self.nodes[target]
                    or edge_variable in self.nodes or edge_variable in self.edges
                ):
                    raise PrototypePublicationError("Unknown, repeated, or incompatible edge binding")
                self.edges[edge_variable] = (label, source, target)
                self.links.add((source, target))
                path += (
                    ("<-" if reverse else "-")
                    + f"[{_quote_graph_identifier(edge_variable)}:{_quote_graph_identifier(label)}]"
                    + ("-" if reverse else "->") + node_text
                )
                current = following
                hop_count += 1
                if hop_count > 8:
                    raise PrototypePublicationError("Queries are limited to eight fixed hops")
            paths.append(path)
            if not self.peek(","):
                break
            self.take(",")
            if len(paths) >= 8:
                raise PrototypePublicationError("Too many MATCH patterns")
        clauses = ["MATCH " + ", ".join(paths)]
        if self.peek("WHERE"):
            self.take("WHERE")
            predicates = []
            while True:
                variable, property_name, expression = self.property()
                if self.peek("IS"):
                    self.take("IS")
                    negated = self.peek("NOT")
                    if negated:
                        self.take("NOT")
                    self.take("NULL")
                    predicates.append(expression + (" IS NOT NULL" if negated else " IS NULL"))
                else:
                    if self.index >= len(self.tokens) or self.tokens[self.index][1] not in (
                        "=", "<>", "!=", "<", ">", "<=", ">=",
                    ):
                        raise PrototypePublicationError("Unsupported predicate comparison")
                    operator = self.tokens[self.index][1]
                    self.index += 1
                    if self.index + 1 < len(self.tokens) and self.tokens[self.index + 1][1] == ".":
                        other, other_property, right = self.property()
                        if (
                            operator != "=" or variable not in self.nodes or other not in self.nodes
                            or property_name != self.schema["nodes"][self.nodes[variable]]["primary_key"]
                            or other_property != self.schema["nodes"][self.nodes[other]]["primary_key"]
                        ):
                            raise PrototypePublicationError("Property joins must equate canonical node identity keys")
                        self.links.add((variable, other))
                    else:
                        right = self.literal()
                    predicates.append(f"{expression} {operator} {right}")
                if not self.peek("AND"):
                    break
                self.take("AND")
            clauses.append("WHERE " + " AND ".join(predicates))
        reachable = {next(iter(self.nodes))}
        while True:
            expanded = reachable | {
                node for pair in self.links if set(pair).intersection(reachable) for node in pair
            }
            if expanded == reachable:
                break
            reachable = expanded
        if reachable != set(self.nodes):
            raise PrototypePublicationError("Disconnected/cartesian patterns require canonical-ID equality joins")
        self.count_gql = " ".join(clauses) + " RETURN count(*) AS observed_count"
        self.take("RETURN")
        selections = []
        while True:
            variable, prop, expression = self.property()
            self.take("AS")
            alias = self.identifier()
            if alias in self.outputs or alias.startswith("__probe_"):
                raise PrototypePublicationError("Duplicate/reserved output alias")
            self.outputs[alias] = (variable, prop)
            selections.append(f"{expression} AS {_quote_graph_identifier(alias)}")
            if not self.peek(","):
                break
            self.take(",")
        for index, (variable, label) in enumerate(self.nodes.items()):
            alias = f"__probe_entity_{index}"
            self.identity_columns[variable] = alias
            primary_key = self.schema["nodes"][label]["primary_key"]
            selections.append(
                f"{_quote_graph_identifier(variable)}.{_quote_graph_identifier(primary_key)}"
                f" AS {_quote_graph_identifier(alias)}"
            )
        clauses.append("RETURN " + ", ".join(selections))
        if self.peek("ORDER"):
            self.take("ORDER")
            self.take("BY")
            ordering = []
            while True:
                alias = self.identifier()
                if alias not in self.outputs:
                    raise PrototypePublicationError("ORDER BY requires a returned property alias")
                direction = ""
                if self.peek("ASC") or self.peek("DESC"):
                    direction = " " + self.tokens[self.index][1].upper()
                    self.index += 1
                ordering.append(_quote_graph_identifier(alias) + direction)
                if not self.peek(","):
                    break
                self.take(",")
            clauses.append("ORDER BY " + ", ".join(ordering))
        self.take("LIMIT")
        if self.index >= len(self.tokens) or self.tokens[self.index][0] != "number":
            raise PrototypePublicationError("LIMIT requires a positive integer")
        raw_limit = self.tokens[self.index][1]
        self.index += 1
        if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= self.max_rows:
            raise PrototypePublicationError("LIMIT exceeds the configured row bound")
        self.limit = int(raw_limit)
        if self.index != len(self.tokens):
            raise PrototypePublicationError("Only one bounded MATCH/WHERE/RETURN/ORDER BY/LIMIT query is allowed")
        # One additional row detects an incomplete answer instead of blessing LIMIT truncation.
        clauses.append(f"LIMIT {self.limit + 1}")
        return " ".join(clauses)


class _Probe(_Run):
    def __init__(self, output: Path, plan: dict[str, Any], report: dict[str, Any], timeout: int) -> None:
        if output.exists():
            raise PrototypePublicationError("Probe output already exists; choose a new output path")
        self.path, self.plan, self.data = output, plan, report
        self.deadline = time.monotonic() + timeout
        self._access_token = None
        self.calls = 0
        self.allowed_items: set[str] = set()
        from azure.identity import AzureCliCredential
        self.credential = AzureCliCredential()
        _atomic_json(output, report, create=True)

    def create(self, *args: Any, **kwargs: Any) -> str:
        raise PrototypePublicationError("A query probe cannot create Fabric items")

    def delta(self, *args: Any, **kwargs: Any) -> None:
        raise PrototypePublicationError("A query probe cannot write Delta tables")

    def token(self) -> str:
        if self._access_token is None or self._access_token.expires_on < time.time() + 60:
            self._access_token = self.credential.get_token("https://api.fabric.microsoft.com/.default")
        return self._access_token.token

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        import requests

        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.calls >= self.plan["graph_readback_policy"]["max_requests"]:
            raise PrototypePublicationError("Probe client timeout/request budget exhausted; no offline fallback")
        parsed = urlparse(url)
        path = parsed.path
        if parsed.scheme != "https" or parsed.netloc != "api.fabric.microsoft.com":
            raise PrototypePublicationError("Cross-origin probe request refused")
        operation = re.fullmatch(r"/v1/operations/[0-9a-fA-F-]+(?:/result)?", path)
        item = re.fullmatch(
            rf"/v1/workspaces/{re.escape(self.plan['workspace_id'])}/"
            r"(lakehouses|ontologies|graphModels)/([0-9a-fA-F-]+)(/getDefinition|/executeQuery)?",
            path,
        )
        permitted = (
            method == "GET" and operation is not None
            or item is not None and item[2] in self.allowed_items
            and (
                method == "GET" and item[3] is None
                or method == "POST" and item[3] in ("/getDefinition", "/executeQuery")
            )
        )
        if not permitted:
            raise PrototypePublicationError("Probe transport forbids item mutation and unrelated targets")
        self.calls += 1
        self.data["fabric_request_count"] = self.calls
        token = self.token()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise PrototypePublicationError("Probe deadline exhausted while obtaining credentials")
        return requests.request(
            method, url, headers={"Authorization": f"Bearer {token}"},
            timeout=max(0.1, min(remaining, 90)), allow_redirects=False, **kwargs,
        )

    def post(self, url: str, _headers: dict[str, str], body: dict[str, Any]) -> _GMResponse:
        response = self.request("POST", url, json=body)
        payload = response.json() if response.content else {}
        if response.status_code != 200:
            self.data.setdefault("query_http_errors", []).append({
                "status_code": response.status_code, "body": payload,
            })
            self.save()
        return _GMResponse(response.status_code, payload, dict(response.headers))


def _schema(definition: dict[str, Any], compilation: Any) -> dict[str, Any]:
    payloads = _definition_payloads(definition)
    sources = {
        item["name"]: item["properties"]["path"].removeprefix("Tables/dbo/")
        for item in payloads["dataSources.json"]["dataSources"]
    }
    node_types = {item["alias"]: item for item in payloads["graphType.json"]["nodeTypes"]}
    edge_types = {item["alias"]: item for item in payloads["graphType.json"]["edgeTypes"]}
    nodes, edges = {}, {}
    aliases = {}
    relationships = {
        item["physical_table_id"]: item["canonical_semantic_relationship_id"]
        for item in compilation.definitions["ontology"]["relationship_types"]
    } | {
        item["physical_table_id"]: item["canonical_relationship_id"]
        for item in compilation.graph_catalog["edges"]
    }
    for binding in payloads["graphDefinition.json"]["nodeTables"]:
        node = node_types[binding["nodeTypeAlias"]]
        label = node["labels"][0]
        if label in nodes:
            raise PrototypePublicationError("Ambiguous node labels cannot be probed")
        aliases[node["alias"]] = label
        nodes[label] = {
            "table_id": sources[binding["dataSourceName"]],
            "primary_key": node["primaryKeyProperties"][0],
            "properties": {item["propertyName"]: item["sourceColumn"] for item in binding["propertyMappings"]},
        }
    for binding in payloads["graphDefinition.json"]["edgeTables"]:
        edge = edge_types[binding["edgeTypeAlias"]]
        label = edge["labels"][0]
        if label in edges:
            raise PrototypePublicationError("Ambiguous edge labels cannot be probed")
        table_id = sources[binding["dataSourceName"]]
        edges[label] = {
            "table_id": table_id, "canonical_relationship_id": relationships[table_id],
            "source_label": aliases[edge["sourceNodeType"]["alias"]],
            "target_label": aliases[edge["destinationNodeType"]["alias"]],
            "properties": {item["propertyName"]: item["sourceColumn"] for item in binding["propertyMappings"]},
        }
    return {"nodes": nodes, "edges": edges}


def _evidence(source: SealedL4ServingSource, l3_root: Path) -> dict[str, Any]:
    roots = []
    for path in sorted(l3_root.rglob("output-manifest.json")):
        try:
            manifest = ArtifactManifest.model_validate_json(path.read_text("utf-8"))
        except ValueError:
            continue
        if manifest == source.input_manifest:
            roots.append(path.parent)
    if len(roots) != 1:
        raise PrototypePublicationError("Evidence requires one unambiguous sealed L3 run")
    root = roots[0]
    partitions = {}
    for entry in source.input_manifest.entries:
        if entry.contract_kind != "c0.evidence_span":
            continue
        batch_id = entry.artifact_id.removesuffix(":evidence")
        path = root / "evidence-spans" / f"{_safe_id(batch_id)}.json"
        if not path.resolve().is_relative_to(root.resolve()):
            raise PrototypePublicationError("Evidence path escaped the sealed run")
        raw = path.read_bytes()
        payload = _strict_json(path)
        if len(raw) != entry.byte_count or raw != (canonical_json(payload) + "\n").encode("utf-8"):
            raise PrototypePublicationError("Evidence partition bytes differ from the sealed format")
        partitions[entry.artifact_id] = tuple(
            EvidenceSpanV1_1.model_validate_json(json.dumps(item)) for item in payload
        )
    return _validate_evidence_partitions(source, partitions)


def _questions(path: Path, max_rows: int) -> list[dict[str, Any]]:
    payload = _strict_json(path)
    if not isinstance(payload, dict) or set(payload) != {"contract_version", "questions"}:
        raise PrototypePublicationError("Questions JSON requires contract_version and questions")
    questions = payload["questions"]
    if payload["contract_version"] != "1.0.0" or not isinstance(questions, list) or len(questions) != 6:
        raise PrototypePublicationError("Acceptance probe requires version 1.0.0 and exactly six questions")
    identifiers = set()
    for item in questions:
        if (
            not isinstance(item, dict)
            or not {"question_id", "question", "query", "expected"}.issubset(item)
            or set(item) - {"question_id", "question", "query", "expected", "derive", "expected_derived"}
        ):
            raise PrototypePublicationError("Each question requires question_id, question, query, expected")
        if ("derive" in item) != ("expected_derived" in item):
            raise PrototypePublicationError("derive and expected_derived must be specified together")
        if "derive" in item and (
            not isinstance(item["expected_derived"], list)
            or not item["expected_derived"]
            or any(not isinstance(row, dict) for row in item["expected_derived"])
        ):
            raise PrototypePublicationError("expected_derived must contain expected derived group objects")
        for field in ("question_id", "question", "query"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise PrototypePublicationError(f"Question {field} must be nonempty text")
        if item["question_id"] in identifiers:
            raise PrototypePublicationError("Question IDs must be unique")
        identifiers.add(item["question_id"])
        expected = item["expected"]
        if not isinstance(expected, dict) or not expected or set(expected) - {
            "min_rows", "max_rows", "contains_rows", "exact_rows",
        }:
            raise PrototypePublicationError("Unsupported or empty expected conditions")
        for field in ("min_rows", "max_rows"):
            if field in expected and (type(expected[field]) is not int or not 0 <= expected[field] <= max_rows):
                raise PrototypePublicationError("Expected row bounds must be integers within --max-rows")
        if expected.get("min_rows", 0) > expected.get("max_rows", max_rows):
            raise PrototypePublicationError("Expected minimum row count exceeds maximum")
        for field in ("contains_rows", "exact_rows"):
            if field in expected and (
                not isinstance(expected[field], list)
                or any(not isinstance(row, dict) or not row for row in expected[field])
            ):
                raise PrototypePublicationError("Expected row conditions require nonempty row objects")
    return questions


def _same_value(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is bool and type(expected) is bool and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return actual == expected
    if isinstance(actual, dict) and isinstance(expected, dict):
        return set(actual) == set(expected) and all(_same_value(actual[key], expected[key]) for key in actual)
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same_value(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return canonical_json(actual) == canonical_json(expected)


def _assertions(rows: list[dict[str, Any]], expected: dict[str, Any]) -> bool:
    if len(rows) < expected.get("min_rows", 0) or len(rows) > expected.get("max_rows", len(rows)):
        return False
    for wanted in expected.get("contains_rows", []):
        if not any(
            all(key in row and _same_value(row[key], value) for key, value in wanted.items())
            for row in rows
        ):
            return False
    if "exact_rows" in expected and not _same_value(rows, expected["exact_rows"]):
        return False
    return True


def _validate_derivation(derivation: Any, query: _Query) -> None:
    if not isinstance(derivation, dict) or set(derivation) - {"group_by", "sum", "count_distinct"}:
        raise PrototypePublicationError("derive supports group_by, sum, count_distinct only")
    groups = derivation.get("group_by", [])
    counts = derivation.get("count_distinct", [])
    sums = derivation.get("sum", [])
    for columns in (groups, counts):
        if (
            not isinstance(columns, list) or any(not isinstance(item, str) for item in columns)
            or len(set(columns)) != len(columns) or not set(columns).issubset(query.outputs)
        ):
            raise PrototypePublicationError("Derivation columns must be unique returned property aliases")
    if not isinstance(sums, list) or not (counts or sums):
        raise PrototypePublicationError("derive requires count_distinct or source-backed sums")
    values = set()
    for spec in sums:
        if not isinstance(spec, dict) or set(spec) != {"value", "unit", "distinct_by"}:
            raise PrototypePublicationError("Each sum requires value, unit, distinct_by")
        value, unit, identities = spec["value"], spec["unit"], spec["distinct_by"]
        if (
            not isinstance(value, str) or not isinstance(unit, str)
            or value not in query.outputs or unit not in groups or value in values
            or not isinstance(identities, list) or not identities
            or any(not isinstance(item, str) for item in identities)
            or len(set(identities)) != len(identities)
            or not set(identities).issubset(query.outputs)
        ):
            raise PrototypePublicationError("SUM must use returned columns, explicit identity keys, and a grouped unit")
        owner = query.outputs[value][0]
        if owner not in query.nodes or not any(
            query.outputs[alias] == (
                owner, query.schema["nodes"][query.nodes[owner]]["primary_key"]
            )
            for alias in identities
        ):
            raise PrototypePublicationError("SUM distinct_by must include the quantity owner's returned canonical ID")
        values.add(value)


def _derive(rows: list[dict[str, Any]], derivation: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        group = {column: row[column] for column in derivation.get("group_by", [])}
        grouped[canonical_json(group)].append(row)
    result = []
    for group_key, members in sorted(grouped.items()):
        aggregate = {
            "group": json.loads(group_key),
            "row_count": len(members),
            "count_distinct": {
                column: len({canonical_json(row[column]) for row in members})
                for column in derivation.get("count_distinct", [])
            },
            "sums": {},
        }
        for spec in derivation.get("sum", []):
            quantities = {}
            unit = members[0][spec["unit"]]
            if not isinstance(unit, str) or not unit.strip():
                raise PrototypePublicationError("SUM requires a nonempty, source-cited unit; no implicit conversion")
            for row in members:
                value = row[spec["value"]]
                if type(value) not in (int, float) or row[spec["unit"]] != unit:
                    raise PrototypePublicationError("SUM encountered nonnumeric quantities or mixed units")
                number = Decimal(str(value))
                if not number.is_finite():
                    raise PrototypePublicationError("SUM encountered a nonfinite quantity")
                identity = canonical_json([row[column] for column in spec["distinct_by"]])
                if identity in quantities and quantities[identity] != number:
                    raise PrototypePublicationError("Conflicting quantities share the same canonical identity and unit")
                quantities[identity] = number
            with localcontext() as context:
                # Float64 decimal encodings plus at most ten million rows fit exactly.
                context.prec = 750
                total = sum(quantities.values(), Decimal(0))
            decimal_text = format(total, "f")
            if "." in decimal_text:
                decimal_text = decimal_text.rstrip("0").rstrip(".")
            if decimal_text == "-0":
                decimal_text = "0"
            aggregate["sums"][spec["value"]] = {
                "value_decimal": decimal_text, "unit": unit,
                "distinct_count": len(quantities), "distinct_by": spec["distinct_by"],
            }
        result.append(aggregate)
    return result


def _citation_index(compilation: Any, schema: dict[str, Any]) -> dict[str, Any]:
    entities = {
        item["entity_id"]: item for item in compilation.tables["l4_semantic_asserted_entities"].to_pylist()
    }
    properties, relationships = defaultdict(list), defaultdict(list)
    for item in compilation.tables["l4_semantic_asserted_properties"].to_pylist():
        properties[(item["entity_id"], item["semantic_property_id"])].append(item)
    for item in compilation.tables["l4_semantic_asserted_relationships"].to_pylist():
        relationships[(item["semantic_relationship_id"], item["source_entity_id"], item["target_entity_id"])].append(item)
    identities, pairs = {}, {}
    for table_id in {
        item["table_id"] for item in [*schema["nodes"].values(), *schema["edges"].values()]
    }:
        table = compilation.tables[table_id]
        key = "__canonical_id" if "__canonical_id" in table.column_names else "entity_id"
        identities[table_id], pairs[table_id] = defaultdict(list), defaultdict(list)
        for item in table.to_pylist():
            identities[table_id][item[key]].append(item)
            if "__source_entity_id" in item:
                pairs[table_id][(item["__source_entity_id"], item["__target_entity_id"])].append(item)
    property_ids = {
        (entity["physical_table_id"], prop["physical_column_id"]): prop["canonical_property_id"]
        for entity in compilation.definitions["ontology"]["entity_types"]
        for prop in entity["properties"]
    }
    return {
        "entities": entities, "properties": properties, "relationships": relationships,
        "identities": identities, "pairs": pairs, "property_ids": property_ids,
        "evidence_spans": {},
    }


def _citations(
    query: _Query, row: dict[str, Any], compilation: Any, spans: dict[str, Any], index: dict[str, Any],
) -> list[dict[str, Any]]:
    entities = index["entities"]
    citations = []

    def attach(
        assertion: dict[str, Any], ids: list[str], *, columns: list[str],
        assertion_id: str, claim_kind: str,
    ) -> None:
        if not ids:
            raise PrototypePublicationError("Returned fact has no sealed evidence spans")
        for evidence_id in ids:
            span = spans.get(evidence_id)
            if (
                span is None or span.verification_status != "verified"
                or span.purpose != L3_EXTRACTION_PURPOSE
                or span.identity.domain_contract_hash != assertion["domain_contract_hash"]
            ):
                raise PrototypePublicationError("Returned fact references missing/unverified sealed evidence")
            if evidence_id not in index["evidence_spans"]:
                index["evidence_spans"][evidence_id] = span.model_dump(mode="json")
        citations.append({
            "origin": "sealed-L3-evidence-linked-by-live-canonical-identity",
            "assertion_id": assertion_id, "assertion_row_hash": assertion["row_hash"],
            "asserted_semantic_id": assertion.get("semantic_property_id")
            or assertion.get("semantic_relationship_id") or assertion.get("most_specific_type_id"),
            "entity_id": assertion.get("entity_id"),
            "source_entity_id": assertion.get("source_entity_id"),
            "target_entity_id": assertion.get("target_entity_id"),
            "answer_columns": columns, "claim_kind": claim_kind,
            "evidence_span_ids": sorted(set(ids)),
        })

    for variable, label in query.nodes.items():
        entity_id = row[query.identity_columns[variable]]
        if not isinstance(entity_id, str) or entity_id not in entities:
            raise PrototypePublicationError("Live canonical entity ID is absent from sealed authority")
        node = query.schema["nodes"][label]
        table = compilation.tables[node["table_id"]]
        matches = index["identities"][node["table_id"]].get(entity_id, [])
        if len(matches) != 1:
            raise PrototypePublicationError("Live entity identity has ambiguous compiled membership")
        entity = entities[entity_id]
        attach(
            entity, entity["evidence_span_ids"], columns=[query.identity_columns[variable]],
            assertion_id=entity_id, claim_kind="derived-entity-identity-lineage",
        )
        for alias, (owner, prop) in query.outputs.items():
            if owner != variable:
                continue
            if row[alias] is None:
                raise PrototypePublicationError("Null/missing properties are not source-cited factual answers")
            column = node["properties"][prop]
            field = table.schema.field(column)
            if _graph_row_fingerprint([{"c0": row[alias]}], [field], from_wire=True) != (
                _graph_row_fingerprint([{"c0": matches[0][column]}], [field], from_wire=False)
            ):
                raise PrototypePublicationError("Returned property value does not match its live identity's sealed value")
            property_id = index["property_ids"].get((node["table_id"], column))
            if property_id:
                proofs = index["properties"].get((entity_id, property_id), [])
                if not proofs or row[alias] is None:
                    raise PrototypePublicationError("A null/unasserted property cannot count as a source-cited answer")
                for proof in proofs:
                    attach(
                        proof, proof["evidence_span_ids"], columns=[alias],
                        assertion_id=proof["property_assertion_id"], claim_kind="asserted-property-value",
                    )
            else:
                evidence_ids = (
                    [entity["label_evidence_span_id"]]
                    if column in ("label", "__label") and entity["label_evidence_span_id"]
                    else entity["evidence_span_ids"]
                )
                attach(
                    entity, evidence_ids, columns=[alias], assertion_id=entity_id,
                    claim_kind="source-mention" if column in ("label", "__label")
                    else "derived-entity-identity-lineage",
                )
    for variable, (label, source_var, target_var) in query.edges.items():
        edge = query.schema["edges"][label]
        source_id = row[query.identity_columns[source_var]]
        target_id = row[query.identity_columns[target_var]]
        matches = index["relationships"].get((edge["canonical_relationship_id"], source_id, target_id), [])
        if not matches:
            raise PrototypePublicationError("Live endpoint pair has no asserted sealed relationship")
        output_columns = [alias for alias, (owner, _) in query.outputs.items() if owner == variable]
        if output_columns:
            table = compilation.tables[edge["table_id"]]
            fields = [table.schema.field(edge["properties"][query.outputs[alias][1]]) for alias in output_columns]
            actual = [{f"c{i}": row[alias] for i, alias in enumerate(output_columns)}]
            compatible = []
            for item in index["pairs"][edge["table_id"]].get((source_id, target_id), []):
                expected = [{
                    f"c{i}": item[edge["properties"][query.outputs[alias][1]]]
                    for i, alias in enumerate(output_columns)
                }]
                if _graph_row_fingerprint(actual, fields, from_wire=True) == _graph_row_fingerprint(expected, fields, from_wire=False):
                    compatible.append(item["__canonical_id"])
            matches = [item for item in matches if item["relationship_id"] in compatible]
            if not matches:
                raise PrototypePublicationError("Returned edge properties do not match a sealed endpoint-pair assertion")
        for proof in matches:
            attach(
                proof, proof["evidence_span_ids"], columns=output_columns,
                assertion_id=proof["relationship_id"], claim_kind="asserted-relationship-endpoint-pair",
            )
    return citations


def query_schema2_prototype(
    *, journal_path: Path, plan_path: Path, materialize_dir: Path,
    l4_run: Path, l3_root: Path, questions_path: Path, output: Path,
    target: str, live: bool, acknowledge_beta: bool,
    max_rows: int = 100, max_pages: int = 100, timeout_seconds: int = 90,
) -> dict[str, Any]:
    if not live or not acknowledge_beta:
        raise PrototypePublicationError("Probe requires explicit --live and --acknowledge-beta; no offline fallback")
    if target not in ("ontology-companion", "independent-graph"):
        raise PrototypePublicationError("Unsupported prototype query target")
    if not 1 <= max_rows <= 10_000_000 or not 1 <= max_pages <= 10_000 or not 1 <= timeout_seconds <= 600:
        raise PrototypePublicationError("Probe bounds exceed supported limits")
    if output.resolve() in {journal_path.resolve(), plan_path.resolve(), questions_path.resolve()}:
        raise PrototypePublicationError("Probe output must not replace its inputs")
    plan, journal = _strict_json(plan_path), _strict_json(journal_path)
    if not isinstance(plan, dict) or not isinstance(journal, dict):
        raise PrototypePublicationError("Plan and journal must be JSON objects")
    plan_body = {key: value for key, value in plan.items() if key != "plan_hash"}
    if (
        plan.get("plan_version") != FORMAT_VERSION or plan.get("mode") != "prototype-create-only"
        or plan.get("plan_hash") != canonical_sha256(plan_body) or plan.get("blockers")
        or journal.get("journal_version") != FORMAT_VERSION
        or journal.get("plan_hash") != plan["plan_hash"]
        or journal.get("run_id") != plan["run_id"] or journal.get("workspace_id") != plan["workspace_id"]
        or journal.get("policy") != "create-only-retain-partial"
        or plan.get("compiler_hash") != _compiler_hash()
    ):
        raise PrototypePublicationError("Unowned, changed, blocked, or incompatible prototype plan/journal")
    policy = plan.get("graph_readback_policy", {})
    if (
        not isinstance(policy, dict)
        or type(policy.get("max_total_rows")) is not int
        or not 1 <= policy["max_total_rows"] <= 10_000_000
        or max_rows > policy["max_total_rows"]
        or type(policy.get("page_size")) is not int
        or not 1 <= policy["page_size"] <= MAX_GRAPH_READBACK_ROWS
        or type(policy.get("max_requests")) is not int
        or not 1 <= policy["max_requests"] <= 100_000
        or type(policy.get("max_pages_per_window")) is not int
        or not 1 <= policy["max_pages_per_window"] <= MAX_GRAPH_READBACK_ROWS + 1
    ):
        raise PrototypePublicationError("Query bounds exceed or lack the approved paged-readback policy")
    compilation = _compile(l4_run, l3_root, plan["workspace_id"], plan["name_prefix"])
    if compilation.provenance != plan["provenance"] or {
        name: _table_proof(table) for name, table in sorted(compilation.tables.items())
    } != plan["tables"]:
        raise PrototypePublicationError("Sealed source/schema/data differ from the approved publication")
    import pyarrow.parquet as pq
    if {path.stem for path in (materialize_dir / "tables").glob("*.parquet")} != set(compilation.tables):
        raise PrototypePublicationError("Materialized tables differ from the approved set")
    for name, table in compilation.tables.items():
        if _table_proof(pq.read_table(materialize_dir / "tables" / f"{name}.parquet")) != _table_proof(table):
            raise PrototypePublicationError("Materialized values/schema changed")
    ids = {}
    for kind in ("lakehouse", "ontology") + (("graph",) if target == "independent-graph" else ()):
        action = journal["actions"].get(f"create:{kind}", {})
        item_id = str(uuid.UUID(action.get("item_id", "")))
        metadata = action.get("metadata", {})
        request = {"displayName": plan["names"][kind], "description": plan["description"]}
        if kind == "lakehouse":
            request["creationPayload"] = {"enableSchemas": True}
        else:
            request["definition"] = _strict_json(materialize_dir / "native-bound" / f"{kind}.json")
        if (
            action.get("status") != "identity-verified" or metadata.get("id") != item_id
            or metadata.get("displayName") != plan["names"][kind]
            or action.get("http_status") not in (200, 201, 202)
            or action.get("http_status") == 202 and action.get("operation_state") != "Succeeded"
            or action.get("http_status") in (200, 201) and action.get("returned_item_id") != item_id
            or action.get("request_hash") != canonical_sha256(request)
            or kind != "lakehouse" and not action.get("definition_readback_hash")
        ):
            raise PrototypePublicationError(f"Journal does not prove exact created {kind} ownership")
        ids[kind] = item_id
    if target == "ontology-companion":
        companions = [
            (key, value) for key, value in journal.get("verified_service_companions", {}).items()
            if value.get("ontology_id") == ids["ontology"] and value.get("lakehouse_id") == ids["lakehouse"]
        ]
        if len(companions) != 1:
            raise PrototypePublicationError("Journal must identify exactly one source-linked Ontology companion")
        graph_id, companion_record = companions[0]
        graph_id = str(uuid.UUID(graph_id))
        graph_hash = companion_record["definition_readback_hash"]
    else:
        graph_id = ids["graph"]
        graph_hash = journal["actions"]["create:graph"]["definition_readback_hash"]
    source = SealedL4ServingSource.from_run(l4_run, input_manifest_search_roots=(l3_root,))
    spans = _evidence(source, l3_root)
    questions = _questions(questions_path, max_rows)
    report = {
        "report_version": "1.1.0", "probe_id": uuid.uuid4().hex, "status": "preflight",
        "live_verified": False, "target_kind": target,
        "ontology_equivalent": False,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "workspace_id": plan["workspace_id"], "ontology_id": ids["ontology"], "graph_id": graph_id,
        "lakehouse_id": ids["lakehouse"], "plan_hash": plan["plan_hash"],
        "journal_hash": canonical_sha256(journal), "questions_hash": canonical_sha256(questions),
        "source_provenance": compilation.provenance, "read_operations": [], "questions": [],
        "bounds": {
            "max_rows": max_rows, "max_pages": max_pages, "client_timeout_seconds": timeout_seconds,
            "approved_total_rows": policy["max_total_rows"],
        },
        "acceptance_scope": "operator-authored assertions; not an NL engine or production release receipt",
        "source_citation_origin": "sealed-L3 evidence; Graph results are live",
    }
    probe = _Probe(output, plan, report, timeout_seconds)
    probe.allowed_items = {*ids.values(), graph_id}
    try:
        for kind, item_id in (("lakehouses", ids["lakehouse"]), ("ontologies", ids["ontology"]), ("graphModels", graph_id)):
            actual = probe.checked(probe.request("GET", f"{API}/workspaces/{plan['workspace_id']}/{kind}/{item_id}"))
            if actual.get("id") != item_id or actual.get("type") != {
                "lakehouses": "Lakehouse", "ontologies": "Ontology", "graphModels": "GraphModel",
            }[kind]:
                raise PrototypePublicationError("Owned item identity changed")
            report.setdefault("item_readbacks", {})[item_id] = actual
            if kind != "graphModels" and actual.get("displayName") != plan["names"]["lakehouse" if kind == "lakehouses" else "ontology"]:
                raise PrototypePublicationError("Owned item name changed")
            if kind == "lakehouses" and actual.get("properties", {}).get("defaultSchema") != "dbo":
                raise PrototypePublicationError("Lakehouse schema changed")
            probe.save()
        native, _ = _native_definitions(
            compilation, workspace_id=plan["workspace_id"], lakehouse_id=ids["lakehouse"],
            names=plan["names"], description=plan["description"], root=materialize_dir,
            semantic_model=plan["semantic_model"],
        )
        ontology_readback = probe.definition("ontology", ids["ontology"])
        report["definition_readbacks"] = {"ontology": ontology_readback}
        probe.save()
        if (
            _definition_payloads(ontology_readback) != _definition_payloads(native["ontology"])
            or canonical_sha256(_definition_payloads(ontology_readback))
            != journal["actions"]["create:ontology"]["definition_readback_hash"]
        ):
            raise PrototypePublicationError("Actual Ontology definition drifted")
        graph = probe.definition("graph", graph_id)
        report["definition_readbacks"]["graph"] = graph
        probe.save()
        if canonical_sha256(_definition_payloads(graph)) != graph_hash:
            raise PrototypePublicationError("Actual Graph definition drifted from the owned journal")
        checks = _graph_readback_checks(
            graph, compilation, plan["workspace_id"], ids["lakehouse"],
            companion=target == "ontology-companion",
            native_ontology=ontology_readback,
        )
        del checks
        schema = _schema(graph, compilation)
        report["schema"] = schema
        probe.save()
        queries = [_Query(item["query"], schema, max_rows) for item in questions]
        for item, query in zip(questions, queries, strict=True):
            for key in ("contains_rows", "exact_rows"):
                if any(set(row) - set(query.outputs) for row in item["expected"].get(key, [])):
                    raise PrototypePublicationError("Expected conditions name non-returned property columns")
            if "derive" in item:
                _validate_derivation(item["derive"], query)
        from deltalake import DeltaTable
        storage_token = probe.credential.get_token("https://storage.azure.com/.default").token
        table_ids = {item["table_id"] for item in schema["nodes"].values()} | {
            item["table_id"] for item in schema["edges"].values()
        }
        for name in sorted(table_ids):
            if time.monotonic() >= probe.deadline:
                raise PrototypePublicationError("Probe client deadline reached during Delta readback")
            uri = f"abfss://{plan['workspace_id']}@onelake.dfs.fabric.microsoft.com/{ids['lakehouse']}/Tables/dbo/{name}"
            action = journal["actions"].get(f"delta:{name}", {})
            if action.get("path") != uri or action.get("status") != "verified" or action.get("table_proof") != plan["tables"][name]:
                raise PrototypePublicationError("Delta table is not verified and owned by this journal")
            delta = DeltaTable(uri, storage_options={"bearer_token": storage_token, "use_fabric_endpoint": "true"})
            history = delta.history(1)
            if (
                delta.version() != 0 or not history
                or history[0].get("prototype_run_id") != plan["run_id"]
                or history[0].get("prototype_plan_hash") != plan["plan_hash"]
                or history[0].get("prototype_action") != f"delta:{name}"
            ):
                raise PrototypePublicationError("Delta commit ownership/version drifted")
            actual = delta.to_pyarrow_dataset().scanner().head(compilation.tables[name].num_rows + 1)
            if _table_proof(actual) != plan["tables"][name]:
                raise PrototypePublicationError("Actual Delta schema/rows/values drifted")
            report.setdefault("delta_readbacks", {})[name] = _table_proof(actual)
            probe.save()
        probe.graph_counts(graph_id, {
            "nodes": sum(compilation.tables[item["table_id"]].num_rows for item in schema["nodes"].values()),
            "edges": sum(compilation.tables[item["table_id"]].num_rows for item in schema["edges"].values()),
        })
        probe.graph_content(
            graph_id, graph, compilation, ids["lakehouse"],
            companion=target == "ontology-companion",
        )
        report["ontology_equivalent"] = target == "ontology-companion"
        citation_index = _citation_index(compilation, schema)
        report["evidence_spans"] = citation_index["evidence_spans"]
        for item, query in zip(questions, queries, strict=True):
            result = {
                **item, "executed_query": query.gql, "query_hash": canonical_sha256(query.gql),
                "identity_columns": query.identity_columns, "status": "running", "pages": [],
                "output_bindings": {
                    alias: {"variable": variable, "graph_property": prop}
                    for alias, (variable, prop) in query.outputs.items()
                },
                "rows": [], "citations": [],
            }
            report["questions"].append(result)
            probe.save()
            count_response = execute_gql_query(
                plan["workspace_id"], graph_id, query.count_gql,
                token_provider=probe.token, transport=probe, beta_acknowledged=True,
            )
            result["completeness_probe"] = {"query": query.count_gql, "response": count_response}
            probe.save()
            count_rows = count_response.get("result", {}).get("data")
            if (
                count_response.get("status", {}).get("code") != "00000"
                or count_response.get("result", {}).get("kind") != "TABLE"
                or not isinstance(count_rows, list) or len(count_rows) != 1
                or not isinstance(count_rows[0], dict)
                or type(count_rows[0].get("observed_count")) is not int
                or count_rows[0]["observed_count"] < 0
                or count_response.get("continuationToken") or count_response.get("continuationUri")
            ):
                raise PrototypePublicationError("Complete-result count probe failed; no bounded prefix can pass")
            expected_rows = count_rows[0]["observed_count"]
            remaining_rows = policy["max_total_rows"] - (
                report.get("graph_readback_row_count", 0) + report.get("question_rows_seen", 0)
            )
            if expected_rows > min(query.limit, remaining_rows):
                raise PrototypePublicationError(
                    f"Complete answer needs {expected_rows} rows, exceeding LIMIT/remaining approved budget; "
                    "all-steps/all-parts prefixes cannot pass"
                )
            continuation = None
            seen = set()
            for _ in range(max_pages):
                body = execute_gql_query(
                    plan["workspace_id"], graph_id, query.gql,
                    token_provider=probe.token, transport=probe,
                    beta_acknowledged=True, continuation_token=continuation,
                )
                response = body.get("result", {})
                page_rows = response.get("data")
                page_record = {
                    "response_hash": canonical_sha256(body), "status": body.get("status"),
                    "columns": response.get("columns"), "row_count": len(page_rows) if isinstance(page_rows, list) else None,
                }
                result["pages"].append(page_record)
                if (
                    body.get("status", {}).get("code") != "00000"
                    or response.get("kind") != "TABLE" or not isinstance(page_rows, list)
                    or body.get("continuationUri") and not body.get("continuationToken")
                    or response.get("continuationToken") or response.get("continuationUri")
                ):
                    page_record["error_response"] = body
                    probe.save()
                    raise PrototypePublicationError("Question query failed, warned, or returned unsupported framing")
                expected_columns = set(query.outputs) | set(query.identity_columns.values())
                if any(not isinstance(row, dict) or set(row) != expected_columns for row in page_rows):
                    raise PrototypePublicationError("Question returned missing or unexpected columns")
                result["rows"].extend(page_rows)
                report["question_rows_seen"] = report.get("question_rows_seen", 0) + len(page_rows)
                probe.save()
                if len(result["rows"]) > query.limit:
                    raise PrototypePublicationError("Question result exceeded its row bound; truncated answers cannot pass")
                if report["question_rows_seen"] + report.get("graph_readback_row_count", 0) > policy["max_total_rows"]:
                    raise PrototypePublicationError("Preflight plus question rows exceeded the approved total readback cap")
                token = body.get("continuationToken")
                if token is not None and not isinstance(token, str):
                    raise PrototypePublicationError("Continuation token must be a string")
                if not token:
                    continuation = None
                    break
                if not isinstance(token, str) or len(token) > 8192 or token in seen:
                    raise PrototypePublicationError("Invalid/repeated continuation token")
                seen.add(token)
                continuation = quote(token, safe="")
            if continuation:
                raise PrototypePublicationError("Question page budget exhausted; incomplete answers cannot pass")
            if len(result["rows"]) != expected_rows:
                raise PrototypePublicationError("Paged answer differs from its full live count; completeness unverified")
            for index, row in enumerate(result["rows"]):
                result["citations"].append({
                    "row_index": index, "links": _citations(query, row, compilation, spans, citation_index),
                })
            answer_rows = [{key: row[key] for key in query.outputs} for row in result["rows"]]
            passed = bool(answer_rows) and _assertions(answer_rows, item["expected"])
            if "derive" in item:
                derived = _derive(answer_rows, item["derive"])
                result["derived"] = {
                    "origin": "complete-live-source-cited-rows",
                    "input_rows_hash": canonical_sha256(answer_rows),
                    "groups": derived,
                }
                passed = passed and _same_value(
                    derived,
                    sorted(item["expected_derived"], key=lambda group: canonical_json(group.get("group"))),
                )
            result["complete_result"] = True
            result["status"] = "operator-assertions-passed" if passed else "assertions-failed-or-no-source-cited-answer"
            result["live_verified"] = True
            probe.save()
        passed = all(item["status"] == "operator-assertions-passed" for item in report["questions"])
        report["live_verified"] = True
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["status"] = (
            "six-question-ontology-probe-passed" if passed and target == "ontology-companion"
            else "independent-graph-probe-passed-not-ontology-acceptance" if passed
            else "question-assertions-failed"
        )
        probe.save()
        return report
    except Exception as error:
        report["status"] = "failed-no-fallback"
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["error"] = {
            "type": type(error).__name__,
            "message": str(error) if isinstance(error, PrototypePublicationError)
            else "Readback/query failed; no offline or alternate-target fallback was used",
        }
        probe.save()
        raise PrototypePublicationError(f"Probe failed; actual evidence retained at {output}: {report['error']['message']}") from error
