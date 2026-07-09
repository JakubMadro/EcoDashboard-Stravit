terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}

provider "azurerm" {
  features {}
}

# =====================================================================
# DEV ENVIRONMENT
# =====================================================================

resource "azurerm_resource_group" "rg_dev" {
  name     = "rg-eco-dashboard-dev"
  location = "switzerlandnorth"
}

resource "azurerm_storage_account" "storage_dev" {
  name                     = "ecodashboardstravitdev"
  resource_group_name      = azurerm_resource_group.rg_dev.name
  location                 = azurerm_resource_group.rg_dev.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
}

resource "azurerm_storage_table" "crews_dev" {
  name                 = "crews"
  storage_account_name = azurerm_storage_account.storage_dev.name
}

resource "azurerm_storage_table" "activities_dev" {
  name                 = "activities"
  storage_account_name = azurerm_storage_account.storage_dev.name
}

resource "azurerm_log_analytics_workspace" "law_dev" {
  name                = "law-eco-dashboard-dev"
  location            = azurerm_resource_group.rg_dev.location
  resource_group_name = azurerm_resource_group.rg_dev.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

resource "azurerm_application_insights" "appins_dev" {
  name                = "appins-eco-dashboard-dev"
  location            = azurerm_resource_group.rg_dev.location
  resource_group_name = azurerm_resource_group.rg_dev.name
  workspace_id        = azurerm_log_analytics_workspace.law_dev.id
  application_type    = "web"
}

resource "azurerm_service_plan" "asp_dev" {
  name                = "asp-eco-dashboard-dev"
  resource_group_name = azurerm_resource_group.rg_dev.name
  location            = azurerm_resource_group.rg_dev.location
  os_type             = "Linux"
  sku_name            = "B1"
}

resource "azurerm_linux_web_app" "web_app_dev" {
  name                = "eco-dashboard-stravit-dev"
  resource_group_name = azurerm_resource_group.rg_dev.name
  location            = azurerm_resource_group.rg_dev.location
  service_plan_id     = azurerm_service_plan.asp_dev.id

  site_config {
    application_stack {
      python_version = "3.12"
    }
    always_on = true
  }

  app_settings = {
    "STRAVIT_EMAIL"                         = var.stravit_email
    "STRAVIT_PASSWORD"                      = var.stravit_password
    "AZURE_STORAGE_CONNECTION_STRING"       = azurerm_storage_account.storage_dev.primary_connection_string
    "APPLICATIONINSIGHTS_CONNECTION_STRING" = azurerm_application_insights.appins_dev.connection_string
    "PORT"                                  = "8000"
    "SCM_DO_BUILD_DURING_DEPLOYMENT"        = "true"
    "SYNC_RATE_LIMIT_MINUTES"               = "30"
  }
}

#resource "azurerm_app_service_source_control" "sc_dev" {
#  app_id                 = azurerm_linux_web_app.web_app_dev.id
#  repo_url               = "https://github.com/JakubMadro/EcoDashboard-Stravit"
#  branch                 = "dev"
#  use_manual_integration = true
#}

# =====================================================================
# PRODUCTION (NORMAL) ENVIRONMENT
# =====================================================================

resource "azurerm_resource_group" "rg_prod" {
  name     = "rg-eco-dashboard-prod"
  location = "switzerlandnorth"
  
  lifecycle {
    ignore_changes = [tags]
  }
}

resource "azurerm_storage_account" "storage_prod" {
  name                     = "ecodashboardstravitprod"
  resource_group_name      = azurerm_resource_group.rg_prod.name
  location                 = azurerm_resource_group.rg_prod.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
}

resource "azurerm_storage_table" "crews_prod" {
  name                 = "crews"
  storage_account_name = azurerm_storage_account.storage_prod.name
}

resource "azurerm_storage_table" "activities_prod" {
  name                 = "activities"
  storage_account_name = azurerm_storage_account.storage_prod.name
}

resource "azurerm_log_analytics_workspace" "law_prod" {
  name                = "law-eco-dashboard-prod"
  location            = azurerm_resource_group.rg_prod.location
  resource_group_name = azurerm_resource_group.rg_prod.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

resource "azurerm_application_insights" "appins_prod" {
  name                = "appins-eco-dashboard-prod"
  location            = azurerm_resource_group.rg_prod.location
  resource_group_name = azurerm_resource_group.rg_prod.name
  workspace_id        = azurerm_log_analytics_workspace.law_prod.id
  application_type    = "web"
}

resource "azurerm_service_plan" "asp_prod" {
  name                = "asp-eco-dashboard-prod"
  resource_group_name = azurerm_resource_group.rg_prod.name
  location            = azurerm_resource_group.rg_prod.location
  os_type             = "Linux"
  sku_name            = "B1"
}

resource "azurerm_linux_web_app" "web_app_prod" {
  name                = "ecodashboardfin"
  resource_group_name = azurerm_resource_group.rg_prod.name
  location            = azurerm_resource_group.rg_prod.location
  service_plan_id     = azurerm_service_plan.asp_prod.id

  site_config {
    application_stack {
      python_version = "3.12"
    }
    always_on = true
  }

  app_settings = {
    "STRAVIT_EMAIL"                         = var.stravit_email
    "STRAVIT_PASSWORD"                      = var.stravit_password
    "AZURE_STORAGE_CONNECTION_STRING"       = azurerm_storage_account.storage_prod.primary_connection_string
    "APPLICATIONINSIGHTS_CONNECTION_STRING" = azurerm_application_insights.appins_prod.connection_string
    "PORT"                                  = "8000"
    "SCM_DO_BUILD_DURING_DEPLOYMENT"        = "true"
    "SYNC_RATE_LIMIT_MINUTES"               = "30"
  }
}

#resource "azurerm_app_service_source_control" "sc_prod" {
#  app_id                 = azurerm_linux_web_app.web_app_prod.id
#  repo_url               = "https://github.com/JakubMadro/EcoDashboard-Stravit"
#  branch                 = "main"
#  use_manual_integration = true
#}
