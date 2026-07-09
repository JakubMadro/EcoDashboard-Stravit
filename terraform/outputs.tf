output "dev_web_app_url" {
  value       = "https://${azurerm_linux_web_app.web_app_dev.default_hostname}"
  description = "The URL of the DEV EcoDashboard web app"
}

output "dev_web_app_name" {
  value       = azurerm_linux_web_app.web_app_dev.name
  description = "The name of the DEV App Service Web App"
}

output "prod_web_app_url" {
  value       = "https://${azurerm_linux_web_app.web_app_prod.name}.azurewebsites.net"
  description = "The URL of the PROD EcoDashboard web app"
}

output "prod_web_app_name" {
  value       = azurerm_linux_web_app.web_app_prod.name
  description = "The name of the PROD App Service Web App"
}
