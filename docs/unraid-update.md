# Mettre à jour WoT Verify sur Unraid

Le script `scripts/update-unraid.sh` automatise la récupération de `main`, la construction de l'image et le remplacement du conteneur **sans Docker Compose**. Il conserve le `.env`, les liaisons SQLite et les paramètres Docker utilisés sur ce serveur : conteneur `wot-verify`, réseau `swagproxy`, stockage `/mnt/user/appdata/discord-wot-verify/data`. Il ne modifie pas SWAG ni les applications Discord/Wargaming.

## Première utilisation

Depuis le terminal Unraid, après fusion de la PR contenant le script :

```bash
cd /mnt/user/appdata/discord-wot-verify
git pull --ff-only origin main
bash scripts/update-unraid.sh
```

Le dépôt doit être sur la branche `main`, sans modifications locales non ignorées. `.env`, `data` et les sauvegardes sont ignorés par Git. Si la branche actuelle est différente, vérifier `git status` avant de passer sur `main` avec `git switch main` ; préserver les éventuelles modifications plutôt que forcer le changement.

Le script nécessite Bash, Git, Docker, `flock`, `tar` et les utilitaires système habituels, avec accès au démon Docker. Le `.env` et le répertoire `data` doivent déjà être préparés selon le README. Le service HTTP doit conserver le port interne **8080** et SQLite doit rester sous `/data`. Le script ne charge jamais `.env` comme du code shell et n'en affiche pas le contenu.

## Mises à jour suivantes

```bash
bash /mnt/user/appdata/discord-wot-verify/scripts/update-unraid.sh
```

1. Un verrou empêche deux mises à jour simultanées. Les prérequis, le réseau, la branche et le montage `/data` du conteneur existant sont vérifiés.
2. `git pull --ff-only origin main` récupère les changements fusionnés. L'image est construite avec un tag distinct pendant que le bot actuel continue à fonctionner. Une erreur Git ou de construction laisse le conteneur actuel intact.
3. Le bot est arrêté, puis le répertoire `data` complet est archivé dans `.deploy-backups/update-XXXXXXXX/data.tar`. Cette sauvegarde à l'arrêt inclut SQLite et ses éventuels fichiers WAL.
4. L'ancien conteneur est conservé sous un nom temporaire. Le nouveau démarre avec le même `.env`, le même montage et les mêmes restrictions Docker.
5. Le script attend jusqu'à 90 secondes la réponse `ready: true` de `/healthz`. Après réussite, l'image reçoit aussi le tag `discord-wot-verify:local` et l'ancien conteneur est retiré.

Le bouton Discord existant reste utilisable ; il n'est pas nécessaire de relancer `/setup`. Une courte interruption intervient entre l'arrêt de l'ancien bot et la connexion du nouveau.

## Déclenchement avec Unraid User Scripts

Créer une entrée **Mettre à jour WoT Verify** dans User Scripts et y coller uniquement ce lanceur :

```bash
#!/bin/bash
exec /bin/bash /mnt/user/appdata/discord-wot-verify/scripts/update-unraid.sh
```

Le chemin absolu permet de lancer la mise à jour quel que soit le répertoire courant choisi par User Scripts. Aucun `cd` préalable n'est nécessaire : le script détermine lui-même le dossier du projet à partir de son propre emplacement. Ne pas copier tout le script de mise à jour dans User Scripts : ce petit lanceur continuera à utiliser la version conservée dans le dépôt.

Exécuter cette entrée après les fusions souhaitées et consulter sa sortie dans User Scripts. `exec` transmet le code de sortie du script : zéro si la mise à jour réussit, une valeur non nulle si elle échoue, même après restauration. Une exécution en arrière-plan convient également ; il ne faut pas ajouter `&` dans le lanceur. Le verrou existant protège contre deux exécutions simultanées.

La première récupération du dépôt décrite plus haut reste nécessaire pour que le fichier existe localement. Les lancements suivants effectuent eux-mêmes le `git pull`. Le script ne crée aucune planification ; si une fréquence est configurée dans User Scripts, chaque lancement construit et remplace le conteneur, même sans nouveau commit.

## Si la mise à jour échoue

Après le début du remplacement, une erreur ou un contrôle de santé négatif déclenche une tentative de retour à l'ancien conteneur. Le script retire le nouveau, conserve les données de la tentative dans le sous-répertoire `failed-data` de la sauvegarde, restaure l'archive SQLite d'avant mise à jour et redémarre l'ancien conteneur. La restauration des données est nécessaire si le nouveau code a déjà appliqué des migrations.

Le script renvoie un code différent de zéro même si la restauration réussit. Si une étape de restauration échoue, son message indique l'action bloquée et l'emplacement de la sauvegarde. Il ne redémarre pas volontairement l'ancien code sur des données dont la restauration a échoué. Une coupure électrique ou un arrêt forcé (`kill -9`) ne permettent pas l'exécution du mécanisme de restauration : examiner les conteneurs et les sauvegardes manuellement avant de relancer.

Le retour arrière concerne le conteneur et les données ; le checkout Git reste à la version récupérée. Il ne faut pas considérer cette procédure comme une sauvegarde des secrets : `.env` doit être sauvegardé séparément et en privé. Les sauvegardes de données contiennent des identifiants personnels et sont créées avec des permissions privées. Elles ne sont pas purgées automatiquement : les supprimer manuellement après validation et selon la rétention souhaitée. Le script ne supprime aucune image Docker.

## Vérifier après mise à jour

```bash
docker logs --tail=50 wot-verify
curl --fail https://discord-auth.battle-conquest.eu/healthz
```

Le script vérifie le service depuis le conteneur ; la commande `curl` ci-dessus vérifie aussi DNS, certificat et SWAG. Tester ensuite le bouton de vérification ou `/resync` dans Discord. Ne pas partager les tokens, le `.env` ou les URL complètes de callback.

## Adapter les paramètres

Les valeurs par défaut conviennent à l'installation actuelle. Si nécessaire, passer les paramètres dans l'environnement du script :

```bash
PROJECT_DIR=/mnt/user/appdata/discord-wot-verify \
DOCKER_NETWORK=swagproxy \
CONTAINER_NAME=wot-verify \
IMAGE_NAME=discord-wot-verify \
HEALTH_TIMEOUT=120 \
bash /mnt/user/appdata/discord-wot-verify/scripts/update-unraid.sh
```

`PROJECT_DIR` désigne le dépôt, avec `.env` et `data`. Par défaut, il est déterminé à partir de l'emplacement du script. `DOCKER_NETWORK` choisit le réseau partagé avec SWAG ; il n'est pas lu depuis `SWAG_NETWORK` dans `.env`, qui reste une variable pour Compose. Si le nom du conteneur change, adapter également la cible du reverse proxy SWAG. Ces variables peuvent aussi être exportées dans le lanceur User Scripts, avant la ligne `exec`.
