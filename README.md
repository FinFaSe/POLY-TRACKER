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

**¿No sabes a quién seguir?** En **Actions → Buscar traders para copiar → Run workflow** (o con `python cli.py top-traders`) verás los mejores traders del ranking de Polymarket. Para cada uno aparecen sus ganancias, su volumen, cuántas operaciones hizo en los últimos 7 días y su dirección, lista para copiar. Marca **solo copiables** (o usa `--solo-copiables`) para ver únicamente traders activos en los últimos 7 días y con ganancias, buscando entre los 100 primeros del ranking: los primeros puestos suelen estar ocupados por traders que ya no operan.
Cómo leer la tabla:
* **⚠️** = cientos de operaciones a la semana (suelen ser bots): casi todas sus copias se descartan por ser demasiado pequeñas respecto a su capital o por llegar tarde.
* **Margen** = ganancias / volumen. Con **↓** (menos de un 3 %), el trader gana poco por cada dólar que mueve, típico de la creación de mercado o el arbitraje: la diferencia entre el precio de compra y el de venta que paga quien copia se come su ganancia.
* Busca ganancias consistentes en varios periodos (`mes`, `semana`, `todo`), margen alto y un número moderado de operaciones.

**¿Le "empujan" el precio a un trader quienes le copian?** En **Actions → Analizar seguidores de un trader** (o con `python cli.py analizar-seguidores --address 0x…`) se revisan sus operaciones recientes y lo que hicieron otras carteras con el mismo resultado justo antes y justo después. El informe muestra si hay **seguidores recurrentes** (carteras que operan en el mismo sentido poco después que él, una y otra vez y en mercados distintos), cuánto mueven frente a él, cómo se mueve el precio tras sus operaciones comparado con la tendencia previa y si vende con ganancia mientras otros siguen comprando. Es una correlación, no una prueba.

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
   * `POLYMARKET_PROXY_ADDRESS` (la dirección de tu cartera de Polymarket; en modo real el bot lee en ella tu saldo de pUSD y tus shares antes de operar)
   * `POLYMARKET_SIGNATURE_TYPE` (opcional, como variable): tipo de firma de las órdenes. `2` (por defecto) para carteras creadas en polymarket.com; `1` para carteras Poly proxy (cuenta de email/Magic); `0` si operas directamente con una wallet propia (EOA).
   * `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASSPHRASE`
   * `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` (opcional para recibir alertas en tu celular).
   * `POLYGON_RPC_URL` (muy recomendable): un RPC de Polygon fiable (Alchemy, Infura, QuickNode…). El bot lo usa para leer el efectivo de cada líder y calcular qué parte de su capital apuesta, y en modo real tu saldo y tus shares. Si no puede leerlo, **no copia la compra**. Sin este secret (o si falla) prueba varios RPCs públicos (`RPC_FALLBACK_URLS`), que funcionan pero tienen límites de uso.
   * `HTTP_PROXY_URL` (obligatorio cuando pases a `DRY_RUN=false` en GitHub Actions, ya que los servidores de GitHub están en EE.UU. y Polymarket bloquea el envío de órdenes desde IPs estadounidenses). Sin él, el bot se niega a enviar órdenes reales desde GitHub Actions.

### Paso 4: Elegir dónde se ejecuta el bot
Elige **una** de estas dos opciones, nunca las dos a la vez: cada una tiene su propia base de datos y copiarían las mismas operaciones dos veces.

* **Opción A — Servidor con Docker (tiempo real, recomendada):** el bot revisa a los traders cada 4 segundos (`POLL_INTERVAL_SECONDS`), así que copia al precio más parecido al del líder. Ver la sección siguiente.
* **Opción B — GitHub Actions (sin servidor, con retraso):** ve a **Actions → Polymarket CopyBot Runner → Run workflow**. Para que se ejecute solo, descomenta las dos líneas de `schedule` en `.github/workflows/copybot.yml`. GitHub no garantiza la puntualidad: con la programación cada 5 minutos suele ejecutarse con 10-30 minutos de retraso, y las compras con más de `MAX_TRADE_AGE_MINUTES` se descartan. Úsala para pruebas o si te vale ese retraso. Ten en cuenta también que las condiciones de GitHub limitan Actions a tareas relacionadas con el desarrollo del software, así que para tenerlo funcionando de forma permanente es preferible un servidor.
  El estado se guarda en `state/copybot.db` dentro del repositorio. Si no se puede guardar (por ejemplo, porque editaste el repositorio mientras corría), el bot reintenta varias veces. Si aun así falla, te avisa por Telegram y el workflow termina en rojo: desactívalo antes de la siguiente ejecución, porque podría repetir órdenes ya enviadas.

GitHub Codespaces no sirve para tenerlo encendido: se apaga solo tras 30 minutos sin actividad.

### Ejecutar en tiempo real en un servidor (Docker)
Necesitas un servidor Linux siempre encendido (un VPS básico de 1 GB de RAM basta) **fuera de EE. UU. y de los demás países donde Polymarket no permite operar**; así no hace falta `HTTP_PROXY_URL`.

1. Instala Docker y clona el repositorio:
   ```bash
   curl -fsSL https://get.docker.com | sh
   git clone https://github.com/FinFaSe/POLY-TRACKER.git && cd POLY-TRACKER
   ```
2. Crea tu configuración a partir del ejemplo y rellénala (empieza con `DRY_RUN=true`):
   ```bash
   cp .env.example .env && nano .env
   ```
3. Arranca el bot y mira sus mensajes:
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
4. En GitHub, crea la variable `BOT_RUNS_ON_SERVER` con valor `true` (**Settings → Secrets and variables → Actions → Variables**). Así el workflow de GitHub Actions no se ejecutará aunque alguien lo lance o reactive su programación.

Cómo funciona en el servidor:
* **Traders:** edita `traders.json` (o haz `git pull` si lo cambias en GitHub). El bot aplica los cambios en el siguiente ciclo, sin reiniciar.
* **Estado:** la base de datos vive en la carpeta `data/` del servidor, no en GitHub. Si antes usaste GitHub Actions y quieres conservar ese estado, copia `state/copybot.db` a `data/copybot.db` antes del primer arranque.
* **Fallos:** si la API de Polymarket o la red fallan, el bot reintenta con esperas crecientes y te avisa por Telegram (una vez, no en cada intento). Si el proceso se cae, Docker lo reinicia solo (`restart: unless-stopped`).
* **Señal de vida:** cada 24 horas (`HEARTBEAT_HOURS`) recibes un aviso de que sigue funcionando. Si deja de llegar, revisa el servidor.
* **Actualizar el código:** `git pull && docker compose up -d --build`.
* **Detenerlo:** `docker compose down` (termina el ciclo en curso antes de salir).

### Modo real: cómo se protegen tus órdenes
Con `DRY_RUN=false`:
* **Saldo real:** antes de cada compra, el bot lee el **pUSD** de tu cartera (`POLYMARKET_PROXY_ADDRESS`). Si no alcanza, no compra y te avisa. Al arrancar también avisa si las sub-bancas suman más dinero del que tienes.
  Desde 2026 Polymarket opera con pUSD en lugar de USDC.e (CLOB V2). En polymarket.com la conversión es automática; si tu cartera aún tiene USDC.e, conviértelo a pUSD desde la web antes de usar el bot en modo real.
* **Ventas:** nunca intenta vender más shares de las que tiene tu cartera (por ejemplo, si vendiste algo a mano).
* **Órdenes:** se envían como órdenes de mercado *Fill-Or-Kill*, con un precio límite igual al del líder más el slippage permitido (`MAX_SLIPPAGE_PCT`). Solo se registran como ejecutadas si Polymarket confirma que se llenaron, y se guardan las cantidades y el precio medio reales. Si la orden queda en espera (`delayed`), se cancela y te avisa para que lo compruebes en polymarket.com.

### Mercados resueltos
Cuando un mercado se resuelve, el bot cierra la posición de la sub-banca automáticamente: abona $1 por share si ganó y $0 si perdió, y te avisa por Telegram. En modo real (`DRY_RUN=false`) el bot **no cobra las ganancias en la blockchain**: entra en polymarket.com y pulsa **Claim** en los mercados ganadores. La frecuencia de esta revisión se ajusta con `RESOLUTION_CHECK_INTERVAL_SECONDS` (por defecto, 300).

### Diagnóstico de las APIs
En **Actions → Diagnóstico de APIs (solo lectura) → Run workflow** puedes comprobar, sin operar, que todo lo que usa el bot responde como espera: la Data API v2 de Polymarket, el CLOB, los RPCs de Polygon y las lecturas en la blockchain. El resultado aparece en el resumen de la ejecución.
