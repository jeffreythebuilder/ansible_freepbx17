# shellcheck shell=sh
# Sourced by interactive login shells, including normal root SSH logins.
# Do not print into scp/sftp or non-interactive remote commands.
case $- in
    *i*)
        if [ -n "${SSH_CONNECTION:-}${SSH_TTY:-}" ] && [ "$(id -u)" -eq 0 ] &&
           [ -x /usr/local/sbin/freepbx-install-status ]; then
            /usr/local/sbin/freepbx-install-status --notice || :
        fi
        ;;
esac
