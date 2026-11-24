package main

// bashCompletion is installed to /usr/share/bash-completion/completions/sparkctl.
const bashCompletion = `# bash completion for sparkctl
_sparkctl() {
  local cur=${COMP_WORDS[COMP_CWORD]}
  if (( COMP_CWORD == 1 )); then
    COMPREPLY=($(compgen -W "status models swap login logout logs doctor version help" -- "$cur"))
    return
  fi
  case ${COMP_WORDS[1]} in
    logs)
      local targets="swapper updater sparkia sparkia:vision sparkia:firewall sparkia:guard"
      if command -v docker >/dev/null 2>&1; then
        targets+=" $(docker ps -a --format 'docker:{{.Names}}' 2>/dev/null)"
      fi
      COMPREPLY=($(compgen -W "$targets" -- "$cur"))
      ;;
    swap)
      COMPREPLY=($(compgen -W "$(sparkctl models 2>/dev/null | awk '{print $2}')" -- "$cur"))
      ;;
    status)
      COMPREPLY=($(compgen -W "--json" -- "$cur"))
      ;;
  esac
}
complete -F _sparkctl sparkctl
`
