#!/bin/sh
set -eu

is_truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|y|on|user|users|multi-user|multi_user|hosted) return 0 ;;
    *) return 1 ;;
  esac
}

is_falsey() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    0|false|no|n|off|disabled|disable) return 0 ;;
    *) return 1 ;;
  esac
}

if is_truthy "${ABSTRACTGATEWAY_USER_AUTH:-}" || is_truthy "${ABSTRACTGATEWAY_AUTH_MODE:-}"; then
  if ! is_falsey "${ABSTRACTGATEWAY_BOOTSTRAP_ADMIN:-1}"; then
    token_file="${ABSTRACTGATEWAY_BOOTSTRAP_ADMIN_TOKEN_FILE:-${ABSTRACTGATEWAY_DATA_DIR:-/data}/auth/bootstrap-admin-token}"
    echo "Preparing Gateway admin user for user-auth mode."
    if is_truthy "${ABSTRACTGATEWAY_BOOTSTRAP_PRINT_TOKEN:-0}"; then
      abstractgateway-config bootstrap-admin --token-file "$token_file" --print-token
    else
      abstractgateway-config bootstrap-admin --token-file "$token_file"
    fi
  fi
fi

exec "$@"
