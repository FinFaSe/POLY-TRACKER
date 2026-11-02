#!/usr/bin/env bash
# Guarda state/ (la base de datos de sub-bancas) en el repositorio.
#
# Si este guardado se pierde, la siguiente ejecución no sabe qué operaciones ya
# copió y las repetiría. Por eso:
#   - si el push falla porque la rama avanzó (p. ej. editaste traders.json mientras
#     corría el bot), trae los cambios y reintenta;
#   - si aun así no lo consigue, avisa por Telegram y termina con error.
#
# Variables opcionales:
#   STATE_BRANCH          rama destino (por defecto, la rama actual)
#   STATE_PUSH_ATTEMPTS   número de intentos (por defecto 5)
#   STATE_RETRY_DELAY     segundos base entre intentos (por defecto 2)
#   TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   para el aviso de fallo
set -uo pipefail

BRANCH="${STATE_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
ATTEMPTS="${STATE_PUSH_ATTEMPTS:-5}"
DELAY="${STATE_RETRY_DELAY:-2}"

alert() {
  echo "::error::$1"
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    curl -sS -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=🤖 Polymarket CopyBot [ERROR]
$1" > /dev/null || true
  fi
}

git add state/
if git diff --staged --quiet; then
  echo "Sin cambios en state/: nada que guardar."
  exit 0
fi

git commit -q -m "chore(state): actualizar estado de sub-bancas [skip ci]"

for attempt in $(seq 1 "$ATTEMPTS"); do
  if git push -q origin "HEAD:${BRANCH}"; then
    echo "Estado guardado en ${BRANCH} (intento ${attempt})."
    exit 0
  fi
  echo "El push falló (intento ${attempt}/${ATTEMPTS}); trayendo cambios de ${BRANCH} y reintentando..."
  sleep $((DELAY * attempt))
  # -X theirs: en un rebase, 'theirs' es nuestro commit de estado. Si otro proceso
  # tocó state/ a la vez, gana la base de datos de esta ejecución, que es la que
  # registra las órdenes que se acaban de enviar.
  if ! git pull -q --rebase -X theirs origin "${BRANCH}"; then
    git rebase --abort 2>/dev/null || true
  fi
done

alert "No se pudo guardar el estado de las sub-bancas tras ${ATTEMPTS} intentos. Desactiva el workflow antes de la próxima ejecución: podría repetir órdenes ya enviadas. Revisa el log de GitHub Actions."
exit 1
