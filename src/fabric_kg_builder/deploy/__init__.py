"""Deployment clients for Fabric Lakehouse, Fabric Ontology, and Azure AI Search.

Uses direct OneLake writes for Lakehouse data, Fabric REST for Ontology
definitions, and azure-search-documents for Search indexes and documents.
Services use DefaultAzureCredential by default with supported API-key
fallbacks.
"""
