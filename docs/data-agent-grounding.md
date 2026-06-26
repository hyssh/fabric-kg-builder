# Data Agent grounding for `kg_ontology`

Auto-generated from the deployed knowledge graph for industry **manufacturing**, business domain **field-service**. Paste these into your Fabric **Data Agent** configuration so NL→GQL returns rows instead of 'no data found'.

---

## 1. Agent instructions (Data Agent → "Additional instructions")

```
You answer questions over a knowledge graph for industry manufacturing, business domain field-service. Follow these rules when generating GQL:

NAMES & TYPES
- Never use exact equality on display_name. Use case-insensitive CONTAINS, e.g.
  WHERE LOWER(n.`display_name`) CONTAINS LOWER("<keyword>").
- Match a short distinguishing keyword, not the user's whole phrase (e.g. "swell" or "expansion", not "swollen battery").
- Valid entity types: Device, DeviceModel, Component, Part, PartNumber, Procedure, Step, Tool, Symptom, Cause, Resolution, Section.

QUERY SHAPE (prefer short paths)
- Prefer SINGLE-HOP queries. Only add a second hop if the first returns rows.
- Never require a 3+ hop conjunctive (comma-joined) pattern; use OPTIONAL MATCH for later hops so one missing edge does not zero the result.
- If a query returns 0 rows, retry with a simpler 1-hop query before giving up.
- Do not retry the same failing pattern repeatedly; after one simpler retry, stop.

PROCEDURES & STEPS
- A named procedure (e.g. "Enclosure Replacement") may be split into sibling procedures like "Removal (Enclosure)" / "Installation (Enclosure)". When the exact name has no steps, broaden: match Procedure display_name CONTAINS the key noun (e.g. "enclosure", "ssd", "kickstand") and return all has_step results.
- For VERBATIM step-by-step instructions, the graph holds short step labels only. Use the AI Search data source (document chunks) for the full instruction text — do NOT expect long instructions from Step.display_name.

FALLBACK
- If the graph returns nothing, say so plainly and offer the AI Search results instead. Do NOT invent entities, IDs, or steps that are not in the result set.
```

> **Connect a second data source.** For "detailed steps" and other verbatim-text questions, add your AI Search index (e.g. `kg-dev-kg-chunks`) to this Data Agent alongside the ontology. The graph answers *structure* (which device has which procedure/part, how many steps); AI Search answers *content* (the actual instructions).

## 2. FIRST — discover the real entity names

User phrases rarely match stored `display_name` exactly (a user says "Surface Pro 10" but the node is "Surface Pro 10th Edition for Business"). Before answering device-specific questions, learn the real names so you can pick the right CONTAINS keyword:

```gql
MATCH (d:`DeviceModel`) RETURN d.`display_name` LIMIT 100
```

Then map the user's term to the closest real name and query with a short, distinguishing CONTAINS keyword (e.g. "pro 10", "laptop 5").

## 3. Entity type descriptions (Data Agent → each entity → description)

- **Device** — entity_id, entity_type, display_name, canonical_key  (~101 instances)
- **DeviceModel** — entity_id, entity_type, display_name, canonical_key  (~329 instances)
- **Component** — entity_id, entity_type, display_name, canonical_key  (~1548 instances)
- **Part** — entity_id, entity_type, display_name, canonical_key  (~1320 instances)
- **PartNumber** — entity_id, entity_type, display_name, canonical_key  (~1212 instances)
- **Procedure** — entity_id, entity_type, display_name, canonical_key  (~1859 instances)
- **Step** — entity_id, entity_type, display_name, canonical_key  (~2400 instances)
- **Tool** — entity_id, entity_type, display_name, canonical_key  (~573 instances)
- **Symptom** — entity_id, entity_type, display_name, canonical_key  (~395 instances)
- **Cause** — entity_id, entity_type, display_name, canonical_key  (~271 instances)
- **Resolution** — entity_id, entity_type, display_name, canonical_key  (~552 instances)
- **Section** — entity_id, entity_type, display_name, canonical_key  (~45 instances)

## 4. Relationship map — use these EXACT edge names in GQL

These are the actual edge names in the deployed graph. Do not guess or abbreviate them (e.g. it is `has_component`, not `supported_model_has_component`):

- `DeviceModel` -[`has_procedure`]-> `Procedure`
- `DeviceModel` -[`has_component`]-> `Component`
- `Procedure` -[`has_step`]-> `Step`
- `DeviceModel` -[`has_part`]-> `Part`
- `DeviceModel` -[`has_symptom`]-> `Symptom`
- `Symptom` -[`resolved_by`]-> `Resolution`
- `Cause` -[`causes`]-> `Symptom`
- `Symptom` -[`remediated_by`]-> `Procedure`
- `Cause` -[`addressed_by`]-> `Resolution`
- `Procedure` -[`uses_tool`]-> `Tool`
- `Step` -[`mentions_component`]-> `Component`
- `Procedure` -[`uses_component`]-> `Component`
- `Component` -[`alias_of`]-> `Component`
- `Step` -[`uses_tool_step_tool`]-> `Tool`
- `Part` -[`has_part_number`]-> `PartNumber`
- `Component` -[`has_part_component_part`]-> `Part`
- `Procedure` -[`references`]-> `Procedure`
- `Step` -[`part_of`]-> `Procedure`
- `Component` -[`has_part_number_component_partnumber`]-> `PartNumber`
- `Procedure` -[`uses`]-> `Part`
- `Procedure` -[`has_resolution`]-> `Resolution`
- `Step` -[`installs_part`]-> `Part`
- `Step` -[`next_step`]-> `Step`
- `Component` -[`references_component_procedure`]-> `Procedure`
- `Procedure` -[`applies_to`]-> `Device`
- `Part` -[`part_of_part_component`]-> `Component`
- `Procedure` -[`includes_symptom`]-> `Symptom`
- `Tool` -[`example_of`]-> `Tool`
- `Procedure` -[`applies_to_procedure_devicemodel`]-> `DeviceModel`
- `Component` -[`compatible_with`]-> `DeviceModel`
- `Part` -[`causes_part_symptom`]-> `Symptom`
- `Procedure` -[`lists`]-> `PartNumber`
- `Device` -[`uses_tool_device_tool`]-> `Tool`
- `PartNumber` -[`includes_part`]-> `Part`
- `Step` -[`targets`]-> `Device`
- `Component` -[`has_symptom_component_symptom`]-> `Symptom`
- `Symptom` -[`indicates`]-> `Symptom`
- `PartNumber` -[`part_number_for_component`]-> `Component`

Ready-to-use single-hop templates:

**Components of a device model**
```gql
MATCH (d:`DeviceModel`)-[:`has_component`]->(c:`Component`)
WHERE LOWER(d.`display_name`) CONTAINS LOWER("pro 10")
RETURN DISTINCT c.`display_name`
```

**Steps of a procedure (broaden by key noun if the exact name has none)**
```gql
MATCH (p:`Procedure`)-[:`has_step`]->(s:`Step`)
WHERE LOWER(p.`display_name`) CONTAINS LOWER("kickstand")
RETURN p.`display_name`, s.`display_name`
```

**Causes of a symptom**
```gql
MATCH (c:`Cause`)-[:`causes`]->(s:`Symptom`)
WHERE LOWER(s.`display_name`) CONTAINS LOWER("expansion")
RETURN DISTINCT c.`display_name`
```

**Root-cause analysis for a symptom (cause + fix + steps in one query)**
```gql
MATCH (s:`Symptom`)
WHERE LOWER(s.`display_name`) CONTAINS LOWER("expansion")
OPTIONAL MATCH (c:`Cause`)-[:`causes`]->(s)
OPTIONAL MATCH (s)-[:`diagnosed_by`]->(dt:`Procedure`)
OPTIONAL MATCH (s)-[:`remediated_by`]->(rp:`Procedure`)
OPTIONAL MATCH (rp)-[:`has_step`]->(st:`Step`)
OPTIONAL MATCH (s)-[:`resolved_by`]->(r:`Resolution`)
RETURN s.`display_name`, c.`display_name`, dt.`display_name`,
       rp.`display_name`, st.`display_name`, r.`display_name`
```

## 5. Example queries (Data Agent → "Example queries")

Use the user's competency questions as few-shots. Map each to a SINGLE-HOP GQL query using the relationship map above and CONTAINS on display_name:

- Why is my Surface battery swollen, and what should a technician check and do before replacing it?
- What can cause battery expansion, how do I diagnose it, and how is it resolved?
- What causes battery overheating, and what is the repair procedure and its steps?
- My Surface battery is leaking - what is the root cause and how is it fixed?
- What should a technician do if the battery is venting?
- There is smoke or sparks from the device battery - what are the causes and what should I do?
- What can cause battery expansion?
- How is a swollen battery resolved?
- What diagnostic test is used for battery issues?
- What procedure remediates battery overheating, and what are its steps?
- What components does the Surface Pro 10 for Business have?
- List the parts of the Display Assembly.
- What part number is the Surflink Screw?
- What part numbers belong to the Surface Laptop 5?
- What steps are in the kickstand replacement procedure for the Surface Go 2?
- What tools does the display replacement procedure need?
- How many steps are in the SSD removal procedure?
- What procedures apply to the Surface Pro for Business 11th Edition with Intel?
- List all the steps in the Battery Replacement Process.
- How many Surface device models are affected by battery expansion?
- List every device model that shares the "battery overheating" symptom.
- For battery expansion, how many causes, diagnostic tests, repair procedures, and resolutions are linked?
- Which components have the most part numbers across the whole catalog?
- What tools are used by the display replacement, and which other procedures use those same tools?
- Which symptoms are linked to the most causes?
- Across all models, which repair procedures have the most steps?
- What part numbers are shared between more than one device model?

---

> Generated by `fabric-kg deploy-ontology --create-data-agent-instruction`. Re-deploys refresh this file to match the live graph.
