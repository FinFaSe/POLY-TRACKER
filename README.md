# Polymarket Copy-Trading Multi-Bank Bot (Listo para GitHub)

Sistema modular en Python para copiar automáticamente a múltiples traders en **Polymarket**, asignando un **bank (sub-banca) independiente** a cada persona seguida y calculando compras y ventas en proporción exacta a sus movimientos.

Esta versión está preparada para **ejecutarse directamente en GitHub** mediante **GitHub Actions** o **GitHub Codespaces**, además de servidores VPS o Docker.

---

## Cómo ejecutarlo directamente en GitHub (Sin instalar nada en tu PC)

### Paso 1: Subir los archivos a tu repositorio de GitHub
1. Crea un repositorio nuevo en GitHub (recomendado: **Privado** para que nadie vea a quién sigues ni el estado de tus sub-bancas).
2. Descomprime este `.zip` y sube todos los archivos a la raíz de tu repositorio (asegúrate de incluir las carpetas `.github/` y `.devcontainer/`).

### Paso 2: Configurar a quién vas a seguir desde la web de GitHub (`traders.json`)
No necesitas abrir una terminal para agregar o modificar tus seguimientos. Abre el archivo `traders.json` directamente en GitHub, haz clic en el ícono del lápiz (**Edit**) y coloca la dirección Proxy de Polymarket de cada persona junto con su `bank` en USDC:

```json
{
  "traders": [
    {
      "alias": "BallenaElecciones",
      "proxy_address": "0x1234567890abcdef1234567890abcdef12345678",
      "bank": 500.0,
      "max_drawdown_pct": 0.30,
      "is_active": true
    },
    {
      "alias": "TraderDeportes",
      "proxy_address": "0xabcdef1234567890abcdef1234567890abcdef12",
      "bank": 250.0,
      "max_drawdown_pct": 0.20,
      "is_active": true
    }
  ]
}
```

### Paso 3: Configurar tus credenciales seguras en GitHub Secrets
En tu repositorio de GitHub, ve a **Settings -> Secrets and variables -> Actions**:
1. En la pestaña **Variables**, puedes crear:
   * `DRY_RUN`: Pon `true` para modo simulación (Paper Trading) o `false` para dinero real.
2. En la pestaña **Secrets**, agrega tus llaves (nunca las escribas en el código):
   * `POLYMARKET_PRIVATE_KEY`
   * `POLYMARKET_PROXY_ADDRESS`
   * `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASSPHRASE`
   * `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` (opcional para recibir alertas en tu celular).
   * `HTTP_PROXY_URL` (importante cuando pases a `DRY_RUN=false` en GitHub Actions, ya que los servidores de GitHub están en EE.UU. y Polymarket bloquea el envío de órdenes desde IPs estadounidenses).

### Paso 4: Ejecutar en GitHub Actions o GitHub Codespaces
* **Opción A — GitHub Actions (Automático y gratuito):**
  Ve a la pestaña **Actions** de tu repositorio, selecciona **Polymarket CopyBot Runner** y haz clic en **Run workflow**. El bot escaneará los movimientos, actualizará la base de datos en `state/copybot.db` y guardará automáticamente el estado de cada `bank` en tu repositorio.
* **Opción B — GitHub Codespaces (En tiempo real cada 4 segundos):**
  En la página principal de tu repositorio, haz clic en el botón verde **<> Code -> pestaña Codespaces -> Create codespace on main**. Se abrirá un entorno en la nube preconfigurado donde puedes correr `python main.py` de forma continua.
