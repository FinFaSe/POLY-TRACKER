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

`traders.json` manda: en cada ejecución el bot lo copia a su base de datos.
* Para dejar de seguir a alguien, pon `"is_active": false` o borra su entrada. Si la borras, el bot lo desactiva y conserva su historial.
* Los comandos `python cli.py add-trader`, `update-bank`, `pause` y `resume` modifican este mismo archivo. Si el bot corre en GitHub Actions, haz commit y push del archivo después de usarlos.

### Paso 3: Configurar tus credenciales seguras en GitHub Secrets
En tu repositorio de GitHub, ve a **Settings -> Secrets and variables -> Actions**:
1. En la pestaña **Variables**, puedes crear:
   * `DRY_RUN`: Pon `true` para modo simulación (Paper Trading) o `false` para dinero real.
   * `MAX_TRADE_AGE_MINUTES` (opcional, por defecto `60`): las compras del líder más antiguas que esto no se copian, porque el precio ya no es el que él pagó. Las ventas de posiciones que tienes sí se ejecutan.
   * `MAX_ACTIVITY_PAGES` (opcional, por defecto `10`): cuántas páginas de 100 operaciones se leen por trader en cada ciclo.
2. En la pestaña **Secrets**, agrega tus llaves (nunca las escribas en el código):
   * `POLYMARKET_PRIVATE_KEY`
   * `POLYMARKET_PROXY_ADDRESS` (la dirección de tu cartera de Polymarket; en modo real el bot lee en ella tu saldo de USDC y tus shares antes de operar)
   * `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASSPHRASE`
   * `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` (opcional para recibir alertas en tu celular).
   * `POLYGON_RPC_URL` (muy recomendable): un RPC de Polygon fiable (Alchemy, Infura, QuickNode…). El bot lo usa para leer el efectivo de cada líder y calcular qué parte de su capital apuesta. Si no puede leerlo, **no copia la compra**. Sin este secret usa `https://polygon-rpc.com`, que es público y a veces falla.
   * `HTTP_PROXY_URL` (obligatorio cuando pases a `DRY_RUN=false` en GitHub Actions, ya que los servidores de GitHub están en EE.UU. y Polymarket bloquea el envío de órdenes desde IPs estadounidenses). Sin él, el bot se niega a enviar órdenes reales desde GitHub Actions.

### Paso 4: Ejecutar en GitHub Actions o GitHub Codespaces
* **Opción A — GitHub Actions (Automático y gratuito):**
  Ve a la pestaña **Actions** de tu repositorio, selecciona **Polymarket CopyBot Runner** y haz clic en **Run workflow**. El bot escaneará los movimientos, actualizará la base de datos en `state/copybot.db` y guardará automáticamente el estado de cada `bank` en tu repositorio.
  Si el estado no se puede guardar (por ejemplo, porque editaste el repositorio mientras corría), el bot reintenta varias veces. Si aun así falla, te avisa por Telegram y el workflow termina en rojo: desactívalo antes de la siguiente ejecución, porque podría repetir órdenes ya enviadas.
* **Opción B — GitHub Codespaces (En tiempo real cada 4 segundos):**
  En la página principal de tu repositorio, haz clic en el botón verde **<> Code -> pestaña Codespaces -> Create codespace on main**. Se abrirá un entorno en la nube preconfigurado donde puedes correr `python main.py` de forma continua.

### Modo real: cómo se protegen tus órdenes
Con `DRY_RUN=false`:
* **Saldo real:** antes de cada compra, el bot lee el USDC de tu cartera (`POLYMARKET_PROXY_ADDRESS`). Si no alcanza, no compra y te avisa. Al arrancar también avisa si las sub-bancas suman más dinero del que tienes.
* **Ventas:** nunca intenta vender más shares de las que tiene tu cartera (por ejemplo, si vendiste algo a mano).
* **Órdenes:** se envían como órdenes de mercado *Fill-Or-Kill*, con un precio límite igual al del líder más el slippage permitido (`MAX_SLIPPAGE_PCT`). Solo se registran como ejecutadas si Polymarket confirma que se llenaron, y se guardan las cantidades y el precio medio reales. Si la orden queda en espera (`delayed`), se cancela y te avisa para que lo compruebes en polymarket.com.

### Mercados resueltos
Cuando un mercado se resuelve, el bot cierra la posición de la sub-banca automáticamente: abona $1 por share si ganó y $0 si perdió, y te avisa por Telegram. En modo real (`DRY_RUN=false`) el bot **no cobra las ganancias en la blockchain**: entra en polymarket.com y pulsa **Claim** en los mercados ganadores. La frecuencia de esta revisión se ajusta con `RESOLUTION_CHECK_INTERVAL_SECONDS` (por defecto, 300).
