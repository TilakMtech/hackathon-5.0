# Deploying the Halcyon OTD Control Tower to Azure

The organisers give each team two resource groups:

| Resource group | Subscription | What goes there |
| --- | --- | --- |
| `rg-csc-blp` | Hackathon 5.0 MPN 2026_2027_09 | The app: Container Apps, Container Registry, Log Analytics, App Insights (this guide) |
| `AI-F-rg-csc-blp` | Hackathon 5.0 EA Subscription | AI services: the Foundry resource `csc-blp-foundry-cus` and its Claude deployments (created in the portal and moved here; see `docs/AI_AGENTS.md`) |

The app reaches Foundry over HTTPS with its key, so the two groups and subscriptions don't need to be linked.
Pick **one** option. Option A is the production-style one; option B reuses the VMs you already have.

---

## Option A: Azure Container Apps with `azd` (recommended)

No Docker needed on your Cloud PC: the images are built inside Azure Container Registry.

### One-time setup on the Cloud PC (PowerShell)

```powershell
winget install Microsoft.AzureCLI
winget install Microsoft.Azd
# close and reopen PowerShell after installing
```

### Deploy

```powershell
cd halcyon-otd                      # the folder that contains azure.yaml
az login                            # sign in with your Evoke account
azd auth login
azd env new csc-blp-otd
azd env set AZURE_SUBSCRIPTION_ID dd8b37ac-5702-4df4-8a27-210a80f8d7f4
azd env set AZURE_RESOURCE_GROUP rg-csc-blp
azd env set AZURE_LOCATION centralus
# To run the agents on Claude, also set these (the dashboard is public, so a key protects the paid endpoints):
# azd env set OTD_LLM_PROVIDER claude_foundry
# azd env set FOUNDRY_RESOURCE csc-blp-foundry-cus
# azd env set FOUNDRY_API_KEY <key from the Foundry portal>
# azd env set OTD_API_KEY <any long random string; share it only with the demo team>
azd up
```

The API container is internal: only the dashboard reaches it. With a real model, the deployment refuses AI calls until `OTD_API_KEY` is set. The dashboard then shows a key field next to the AI badge. AI calls share a spend cap of 30 a minute, and eval runs are limited to 3 an hour, because each makes about 400 model calls. Analytics tabs stay open. This Bicep has not been deployed yet, so treat the first `azd up` as a test.

`azd up` takes about 8–12 minutes the first time. At the end it prints two URLs:

- `SERVICE_WEB_URI` is the dashboard to open in the browser and show the judges.
- `SERVICE_API_URI` is the API's internal address (not reachable from the internet).

### What gets created in rg-csc-blp

| Resource | Name starts with | Why |
| --- | --- | --- |
| Log Analytics workspace | `log-otd-` | Container logs |
| Application Insights | `appi-otd-` | Monitoring (and LLM tracing in slice 2) |
| Container Registry (Basic) | `crotd` | Holds the two images |
| Container Apps environment | `cae-otd-` | Hosts the apps |
| Container App | `ca-otd-api-` | FastAPI analytics API |
| Container App | `ca-otd-web-` | React dashboard behind nginx |

Rough cost: under $2 a day with one replica each. Run `azd down` after the hackathon to delete only these resources.

### If something fails

| Error mentions | Fix |
| --- | --- |
| `AuthorizationFailed` / `does not have authorization` | Your role on rg-csc-blp is below Contributor. Ask the admin for **Contributor** on the resource group. |
| `MissingSubscriptionRegistration` for `Microsoft.App` or `Microsoft.ContainerRegistry` | Ask the admin to register those resource providers on the subscription (Subscriptions → Resource providers → Register). |
| `TasksOperationsNotAllowed` or remote build fails | ACR Tasks are blocked on this subscription. Install Docker Desktop, set `remoteBuild: false` for both services in `azure.yaml`, and run `azd deploy` again. |
| Region not allowed for Container Apps | `azd env set AZURE_LOCATION eastus2` (or `westus3`) and run `azd up` again. |

---

## Option B: run it on one of the existing VMs

Use this if you cannot create new resources.

1. Open `csc-blp-VM` in the portal. Under **Networking**, add an inbound rule that allows **port 80** from your IP only.
2. Connect to the VM (Connect → SSH or Bastion). If it is a Linux VM:

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
git clone <your GitHub repo URL> && cd halcyon-otd
sudo docker compose up -d --build
```

3. Open `http://<VM public IP>` in the browser.

If the VM is Windows, tell Claude and I'll give you the Windows steps.

---

## Run it locally (any machine with Python 3.12 and Node 22)

```bash
# terminal 1
cd backend && pip install -r requirements-dev.txt && uvicorn app.main:app --reload --reload-dir app --port 8000
# terminal 2
cd frontend && npm ci && npm run dev        # open http://localhost:5173
```

Tests: `cd backend && pytest -q`
