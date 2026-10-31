# Configuración de GitHub

La subcarpeta `workflows/` contiene las automatizaciones de GitHub Actions:

- `ci_test.yml`: instala las dependencias y ejecuta las pruebas.
- `copybot.yml`: ejecuta el bot y guarda el estado de las sub-bancas.

GitHub Actions requiere que estos archivos estén en `.github/workflows/`.
