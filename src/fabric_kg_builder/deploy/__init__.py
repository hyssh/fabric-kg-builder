"""Deployment clients for Fabric Lakehouse, Fabric Ontology, and Azure AI Search.

Uses fabric-cicd for ontology deployment and direct Fabric REST API
for lakehouse data upload. Azure AI Search index deployment via
azure-search-documents SDK. All Azure services authenticate via
DefaultAzureCredential; Foundry uses explicit API key from .env.
"""
