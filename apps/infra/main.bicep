targetScope = 'resourceGroup'

param imageTag string
param acrLoginServer string
param acrName string
param acrResourceId string
param acrRepository string
param environment string
param baseName string
param runId string = ''
param tenantId string
param apiAudience string
param apiScope string
param requiredApiAppRole string = ''
param additionalAllowedCallerObjectIds string = ''
param searchEndpoint string
param searchIndexName string
param visualIndexName string
param blobAccountUrl string
param blobContainer string
param storageAccountResourceId string
param searchServiceResourceId string
param fabricWorkspaceId string
param graphModelId string
param graphPreviewAcknowledged bool
param createManagedIdentity bool = false
param managedIdentityName string
param managedIdentityResourceId string = ''
param managedIdentityClientId string = ''
param managedIdentityPrincipalId string = ''
param downstreamAccessConfirmed bool
param apiAppRoleGrantConfirmed bool
param querySchemaMode string
param querySchemaPath string = ''
param querySchemaHash string = ''
param queryAuthorityHash string = ''
param domainContractHash string = ''
param approvedMaxHops int = 0

var safeSuffix = toLower(replace(imageTag, 'T', '-'))
var apiName = '${baseName}-${environment}-api'
var uiName = '${baseName}-${environment}-ui'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (createManagedIdentity) {
  name: managedIdentityName
  location: resourceGroup().location
}

var acrResourceSegments = split(acrResourceId, '/')

module createdIdentityAcrPull './acr-role.bicep' = if (createManagedIdentity) {
  name: 'acr-pull-created-${uniqueString(acrResourceId, managedIdentityName)}'
  scope: resourceGroup(
    acrResourceSegments[2],
    acrResourceSegments[4]
  )
  params: {
    acrName: acrName
    principalId: identity!.properties.principalId
    assignmentSeed: identity!.name
  }
}

module existingIdentityAcrPull './acr-role.bicep' = if (!createManagedIdentity) {
  name: 'acr-pull-existing-${uniqueString(acrResourceId, managedIdentityPrincipalId)}'
  scope: resourceGroup(
    acrResourceSegments[2],
    acrResourceSegments[4]
  )
  params: {
    acrName: acrName
    principalId: managedIdentityPrincipalId
    assignmentSeed: managedIdentityPrincipalId
  }
}

var runtimeIdentityId = createManagedIdentity
  ? identity!.id
  : managedIdentityResourceId
var runtimeIdentityClientId = createManagedIdentity
  ? identity!.properties.clientId
  : managedIdentityClientId

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${baseName}-${environment}-env'
  location: resourceGroup().location
  tags: {
    runId: runId
    storageAccountResourceId: storageAccountResourceId
    searchServiceResourceId: searchServiceResourceId
    managedIdentityPrincipalId: managedIdentityPrincipalId
    downstreamAccessConfirmed: string(downstreamAccessConfirmed)
    apiAppRoleGrantConfirmed: string(apiAppRoleGrantConfirmed)
  }
}

resource api 'Microsoft.App/containerApps@2025-01-01' = {
  name: apiName
  location: resourceGroup().location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${runtimeIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
      }
      registries: [
        {
          server: acrLoginServer
          identity: runtimeIdentityId
        }
      ]
    }
    template: {
      revisionSuffix: safeSuffix
      containers: [
        {
          name: 'api'
          image: '${acrLoginServer}/${acrRepository}/api:${imageTag}'
          env: [
            { name: 'FABRIC_KG_ENVIRONMENT', value: environment }
            { name: 'FABRIC_KG_TENANT_ID', value: tenantId }
            { name: 'FABRIC_KG_AUDIENCE', value: apiAudience }
            { name: 'FABRIC_KG_REQUIRED_APP_ROLE', value: requiredApiAppRole }
            { name: 'FABRIC_KG_ALLOWED_CALLER_OBJECT_IDS', value: additionalAllowedCallerObjectIds }
            { name: 'FABRIC_KG_SEARCH_ENDPOINT', value: searchEndpoint }
            { name: 'FABRIC_KG_KB_INDEX', value: searchIndexName }
            { name: 'FABRIC_KG_VISUAL_INDEX', value: visualIndexName }
            { name: 'FABRIC_KG_BLOB_ACCOUNT_URL', value: blobAccountUrl }
            { name: 'FABRIC_KG_BLOB_CONTAINER', value: blobContainer }
            { name: 'FABRIC_KG_FABRIC_WORKSPACE_ID', value: fabricWorkspaceId }
            { name: 'FABRIC_KG_GRAPH_MODEL_ID', value: graphModelId }
            { name: 'FABRIC_KG_MANAGED_IDENTITY_CLIENT_ID', value: runtimeIdentityClientId }
            { name: 'FABRIC_KG_GRAPH_PREVIEW_ACKNOWLEDGED', value: string(graphPreviewAcknowledged) }
            { name: 'FABRIC_KG_QUERY_SCHEMA_MODE', value: querySchemaMode }
            { name: 'FABRIC_KG_QUERY_SCHEMA_PATH', value: querySchemaPath }
            { name: 'FABRIC_KG_QUERY_SCHEMA_HASH', value: querySchemaHash }
            { name: 'FABRIC_KG_QUERY_AUTHORITY_HASH', value: queryAuthorityHash }
            { name: 'FABRIC_KG_DOMAIN_CONTRACT_HASH', value: domainContractHash }
            {
              name: 'FABRIC_KG_APPROVED_MAX_HOPS'
              value: approvedMaxHops == 0 ? '' : string(approvedMaxHops)
            }
          ]
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/ready'
                port: 8000
              }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
  dependsOn: [
    createdIdentityAcrPull
    existingIdentityAcrPull
  ]
}

resource ui 'Microsoft.App/containerApps@2025-01-01' = {
  name: uiName
  location: resourceGroup().location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${runtimeIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 80
        transport: 'http'
      }
      registries: [
        {
          server: acrLoginServer
          identity: runtimeIdentityId
        }
      ]
    }
    template: {
      revisionSuffix: safeSuffix
      containers: [
        {
          name: 'ui'
          image: '${acrLoginServer}/${acrRepository}/ui:${imageTag}'
          env: [
            { name: 'API_SCOPE', value: apiScope }
            { name: 'API_URL', value: 'https://${api.properties.configuration.ingress.fqdn}' }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 2
      }
    }
  }
  dependsOn: [
    createdIdentityAcrPull
    existingIdentityAcrPull
  ]
}

output apiContainerAppId string = api.id
output uiContainerAppId string = ui.id
output apiRevisionName string = '${apiName}--${safeSuffix}'
output uiRevisionName string = '${uiName}--${safeSuffix}'
output apiFqdn string = api.properties.configuration.ingress.fqdn
output uiFqdn string = ui.properties.configuration.ingress.fqdn
output persistedQuerySchemaHash string = querySchemaHash
output boundedQueryAuthorityHash string = queryAuthorityHash
output approvedDomainContractHash string = domainContractHash
