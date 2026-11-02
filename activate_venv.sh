#!/usr/bin/env bash
# Спільна активація venv для всіх Cl-Apps (запуск.sh).
# Шлях до venv зберігається в .cl_apps_venv після setup_linux.sh.

_cl_apps_repo_root() {
  local here="${BASH_SOURCE[1]:-${BASH_SOURCE[0]}}"
  here="$(cd "$(dirname "$here")" && pwd)"
  if [[ -f "$here/activate_venv.sh" ]]; then
    echo "$here"
    return
  fi
  if [[ -f "$here/../activate_venv.sh" ]]; then
    cd "$here/.." && pwd
    return
  fi
  echo "$here"
}

activate_cl_apps_venv() {
  local root
  root="$(_cl_apps_repo_root)"
  local venv=""

  if [[ -n "${CL_APPS_VENV:-}" && -f "${CL_APPS_VENV}/bin/activate" ]]; then
    venv="$CL_APPS_VENV"
  elif [[ -f "$root/.cl_apps_venv" ]]; then
    venv="$(tr -d '\r' <"$root/.cl_apps_venv")"
  elif [[ -f "$root/.venv/bin/activate" ]]; then
    venv="$root/.venv"
  else
    local fallback="${XDG_DATA_HOME:-$HOME/.local/share}/cl-apps-linux/venv"
    if [[ -f "$fallback/bin/activate" ]]; then
      venv="$fallback"
    fi
  fi

  if [[ -n "$venv" && -f "$venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$venv/bin/activate"
  fi
}
