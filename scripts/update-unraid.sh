#!/usr/bin/env bash
# Mise à jour Docker sans Compose. Voir docs/unraid-update.md.
set -Eeuo pipefail
# Git doit créer du code lisible par l'utilisateur non root de l'image.
umask 022

main() {
    local project_dir container network image timeout candidate previous backup=""
    local old_exists=0 old_stopped=0 old_renamed=0 new_started=0
    project_dir="${PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}"
    container="${CONTAINER_NAME:-wot-verify}"
    network="${DOCKER_NETWORK:-swagproxy}"
    image="${IMAGE_NAME:-discord-wot-verify}"
    timeout="${HEALTH_TIMEOUT:-90}"

    fail() { printf 'Erreur : %s\n' "$*" >&2; exit 1; }
    for command in git docker flock tar mktemp; do
        command -v "$command" >/dev/null || fail "Commande manquante : $command"
    done
    [[ "$timeout" =~ ^[1-9][0-9]*$ ]] || fail "HEALTH_TIMEOUT doit être un entier positif."
    cd -- "$project_dir"
    project_dir="$(pwd -P)"
    (umask 077; touch "$project_dir/.unraid-update.lock")
    exec 9>>"$project_dir/.unraid-update.lock"
    flock -n 9 || fail "Une mise à jour est déjà en cours."
    [[ -f .env ]] || fail "Fichier .env manquant ; reprendre .env.example."
    [[ -d data && ! -L data ]] || fail "Le répertoire data doit exister et ne pas être un lien symbolique."
    [[ "$(git branch --show-current)" == main ]] || fail "Passer sur main avant la mise à jour."
    [[ -z "$(git status --porcelain)" ]] || fail "Le dépôt contient des modifications locales ; les préserver avant de continuer."
    docker info >/dev/null
    docker network inspect "$network" >/dev/null

    # Une liste réussie distingue l'absence d'un conteneur d'une panne Docker.
    container_exists() {
        local names name
        names="$(docker container ls --all --format '{{.Names}}')" || return 2
        while IFS= read -r name; do
            [[ "$name" == "$container" ]] && return 0
        done <<< "$names"
        return 1
    }
    if container_exists; then
        old_exists=1
        local mounted_data
        mounted_data="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$container")"
        [[ "$mounted_data" == "$project_dir/data" ]] || fail "Le conteneur existant ne monte pas $project_dir/data ; vérifier PROJECT_DIR et CONTAINER_NAME."
    else
        [[ $? == 1 ]] || fail "Impossible de vérifier les conteneurs Docker."
    fi

    printf 'Récupération de main…\n'
    git pull --ff-only origin main
    candidate="$image:update-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    previous="$container-previous-$$"
    printf 'Construction de %s ; le bot actuel reste disponible.\n' "$candidate"
    docker build --tag "$candidate" .

    # Le corps de main est déjà chargé : git pull peut mettre à jour ce script sans
    # modifier les instructions de la mise à jour en cours.
    rollback() {
        local code="$1"
        trap - ERR INT TERM
        set +e
        printf 'Mise à jour interrompue ; tentative de restauration…\n' >&2
        if (( new_started )); then
            if container_exists; then
                if ! docker rm --force "$container" >/dev/null; then
                    printf 'Impossible de retirer le nouveau conteneur. Aucune restauration des données effectuée.\n' >&2
                    exit "$code"
                fi
            elif [[ $? != 1 ]]; then
                printf 'État Docker inconnu. Aucune restauration des données effectuée ; sauvegarde : %s\n' "$backup" >&2
                exit "$code"
            fi
        fi
        if [[ -n "$backup" && -f "$backup/data.tar" ]]; then
            # Conserver les données de la tentative ratée plutôt que les supprimer.
            if ! mv -- "$project_dir/data" "$backup/failed-data" ||
               ! tar -xpf "$backup/data.tar" -C "$project_dir"; then
                printf 'Restauration des données impossible. Sauvegarde : %s\n' "$backup" >&2
                exit "$code"
            fi
        fi
        if (( old_renamed )); then
            if ! docker rename "$previous" "$container"; then
                printf 'Renommage de restauration impossible : %s\n' "$previous" >&2
                exit "$code"
            fi
        fi
        if (( old_stopped )); then
            docker start "$container" >/dev/null ||
                printf 'Redémarrage précédent impossible ; vérifier docker logs %s.\n' "$container" >&2
        fi
        printf 'Échec de mise à jour. Vérifier le conteneur et la sauvegarde %s.\n' "${backup:-non créée}" >&2
        exit "$code"
    }
    trap 'rollback $?' ERR
    trap 'rollback 130' INT
    trap 'rollback 143' TERM

    if (( old_exists )); then
        old_stopped=1
        docker stop --time 30 "$container" >/dev/null
    fi
    (umask 077; mkdir -p .deploy-backups)
    backup="$(mktemp -d "$project_dir/.deploy-backups/update-XXXXXXXX")"
    # SQLite et ses fichiers WAL sont copiés ensemble, une fois le bot arrêté.
    # Renommer le fichier seulement après une archive terminée avec succès.
    (umask 077; tar -cpf "$backup/data.tar.partial" -C "$project_dir" data)
    mv -- "$backup/data.tar.partial" "$backup/data.tar"
    if (( old_exists )); then
        docker rename "$container" "$previous"
        old_renamed=1
    fi
    new_started=1
    docker run -d \
        --name "$container" \
        --restart unless-stopped \
        --network "$network" \
        --env-file "$project_dir/.env" \
        --mount "type=bind,src=$project_dir/data,dst=/data" \
        --read-only --tmpfs /tmp --cap-drop ALL \
        --security-opt no-new-privileges:true \
        "$candidate" >/dev/null

    printf 'Attente de la connexion Discord, au maximum %s secondes…\n' "$timeout"
    local deadline=$((SECONDS + timeout)) ready=0
    while (( SECONDS < deadline )); do
        if docker exec "$container" python -c \
            'import json, urllib.request; response = urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=3); assert json.load(response).get("ready") is True' \
            >/dev/null 2>&1; then
            ready=1
            break
        fi
        [[ "$(docker inspect --format '{{.State.Running}}' "$container")" == true ]] || break
        sleep 2
    done
    if (( ! ready )); then
        printf 'Le nouveau bot ne répond pas ready:true.\n' >&2
        rollback 1
    fi
    docker tag "$candidate" "$image:local"
    trap - ERR INT TERM
    if (( old_renamed )); then
        docker rm "$previous" >/dev/null ||
            printf 'Nettoyage à effectuer manuellement : docker rm %s\n' "$previous" >&2
    fi
    printf 'Mise à jour réussie. Sauvegarde des données : %s/data.tar\n' "$backup"
    printf 'Contrôle HTTPS : curl --fail https://discord-auth.battle-conquest.eu/healthz\n'
}

main "$@"
