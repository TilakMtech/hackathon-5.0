// Halcyon OTD Control Tower - Azure infrastructure (slice 1: analytics API + dashboard)
// Deploys into an EXISTING resource group (e.g. rg-csc-blp), so Contributor on the group is enough.
targetScope = 'resourceGroup'

@description('Short environment name, used in resource names (set by azd).')
param environmentName string

@description('Azure region. Defaults to the resource group region.')
param location string = resourceGroup().location

@description('Pull images with ACR admin credentials (works with Contributor). Set false to use a managed identity + AcrPull role (needs Owner or User Access Administrator).')
param useAcrAdmin bool = true

@description('Images are set by azd deploy; these placeholders let the first provision succeed.')
param apiImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
param webImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

var token = toLower(uniqueString(subscription().id, resourceGroup().id, environmentName))
var tags = { 'azd-env-name': environmentName, project: 'halcyon-otd', team: 'csc-blp' }

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-otd-${token}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-otd-${token}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'crotd${token}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: useAcrAdmin }
}

resource pullIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (!useAcrAdmin) {
  name: 'id-otd-${token}'
  location: location
  tags: tags
}

// AcrPull built-in role
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!useAcrAdmin) {
  name: guid(acr.id, 'acrpull', environmentName)
  scope: acr
  properties: {
    principalId: pullIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-otd-${token}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

var registries = useAcrAdmin
  ? [{ server: acr.properties.loginServer, username: acr.listCredentials().username, passwordSecretRef: 'acr-password' }]
  : [{ server: acr.properties.loginServer, identity: pullIdentity.id }]
var registrySecrets = useAcrAdmin ? [{ name: 'acr-password', value: acr.listCredentials().passwords[0].value }] : []
var identityBlock = useAcrAdmin ? { type: 'None' } : { type: 'UserAssigned', userAssignedIdentities: { '${pullIdentity.id}': {} } }

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-otd-api-${token}'
  location: location
  tags: union(tags, { 'azd-service-name': 'api' })
  identity: identityBlock
  dependsOn: [acrPull]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        corsPolicy: { allowedOrigins: ['*'], allowedMethods: ['GET', 'POST'] }
      }
      registries: registries
      secrets: registrySecrets
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: [
            { name: 'OTD_CORS_ORIGINS', value: '*' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: { path: '/api/health', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 10
              failureThreshold: 12
            }
          ]
        }
      ]
      // keep one replica warm so the demo never hits a cold start (model fit takes ~10 s)
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

resource web 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-otd-web-${token}'
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  identity: identityBlock
  dependsOn: [acrPull]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: { external: true, targetPort: 80, transport: 'auto' }
      registries: registries
      secrets: registrySecrets
    }
    template: {
      containers: [
        {
          name: 'web'
          image: webImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [{ name: 'API_URL', value: 'https://${api.properties.configuration.ingress.fqdn}' }]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 2 }
    }
  }
}

output AZURE_LOCATION string = location
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = acr.properties.loginServer
output AZURE_CONTAINER_REGISTRY_NAME string = acr.name
output SERVICE_API_URI string = 'https://${api.properties.configuration.ingress.fqdn}'
output SERVICE_WEB_URI string = 'https://${web.properties.configuration.ingress.fqdn}'
