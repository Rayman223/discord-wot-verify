# Anniversaires des comptes World of Tanks

WoT Verify peut remplacer le traitement anniversaire de l'ancien bot. La fonction
est désactivée par défaut : `BIRTHDAY_CHANNEL_ID=0`. La collecte des dates WG
fonctionne également pendant cette préparation, sans modifier les pseudos ni
publier d'annonces.

## Comportement

Le bot vérifie chaque minute si le traitement quotidien est dû, à **7 h dans
`Europe/Brussels`**, avec les changements CET/CEST. Aucun cron ni User Script
quotidien supplémentaire n'est nécessaire. Les dates de création WG restent des
timestamps UTC ; leur jour et mois sont calculés à Bruxelles. Un compte créé le
29 février est célébré le 1er mars les années non bissextiles.

Seuls les membres présents, liés en base et ayant le rôle joueur sont célébrés,
à partir du premier anniversaire. Le pseudo devient `PseudoWG [TAG] 🕯…`, avec
une bougie par année révolue et sans `[TAG]` pour les non-contacts. Les bougies
existantes sont remplacées, pas cumulées. L'identité et le tag ne sont jamais
tronqués : si l'ensemble dépasse 32 caractères, le bot essaie la forme compacte
`PseudoWG [TAG] 🕯x14` pour un compte de 14 ans. Si cette forme dépasse encore
32 caractères, le traitement anniversaire laisse le pseudo inchangé et un
avertissement `nickname_too_long` indique l'ID Discord.
L'annonce reste publiée même si Discord refuse le renommage.

Dans le salon configuré, l'annonce regroupe les anniversaires et mentionne
réellement les membres concernés, par exemple :

> Voici les anniversaires du jour ! 🎂
> • @joueur 🕯🕯 fête ses 2 ans sur World of Tanks ! 🥳

Les annonces suivent `LOCALE` (`fr` ou `en`), restent dans le salon et sont
réparties en plusieurs messages si nécessaire. Aucun message n'est envoyé les
jours sans anniversaire ; aucun rôle ni `@everyone` n'est mentionné.

Les bougies sont retirées le lendemain à 7 h. Après un arrêt de plusieurs jours,
le bot nettoie les décorations expirées, y compris avant 7 h au redémarrage ;
les bougies de la veille restent jusqu'à leur échéance normale. Il annonce
uniquement le jour courant, après 7 h, sans rattraper les journées manquées.
Les nouveaux membres validés et les erreurs individuelles sont réexaminés dans
les quinze minutes. `/resync` et les changements de contact préservent les
bougies dues ; les comparaisons de pseudo les ignorent. Le retrait du rôle joueur
ou de la liaison déclenche aussi leur nettoyage, sans réattribuer de rôle.

## Données et reprise

La migration automatique et idempotente `010_birthdays.sql` ajoute les dates par
compte/région, le suivi des décorations et le journal des annonces. Elle ne modifie
pas les liaisons existantes. Les vérifications, imports et synchronisations
collectent les dates manquantes ; un traitement complète les liaisons existantes
au démarrage puis toutes les quinze minutes, par lots de 100 maximum et avec le
limiteur WG partagé. Une date inconnue, invalide ou future n'est jamais inventée
et ne bloque pas la vérification. Une réponse indisponible n'efface pas une date
valide. Les comptes d'une autre région que `WG_REGION` ne sont pas interrogés
sur le mauvais serveur.

Le suivi du nettoyage est enregistré avant un renommage et conservé après
déliaison si Discord échoue. Les échecs n'arrêtent pas les autres membres.
Un refus explicite de publication permet de réessayer après correction des
permissions ; seuls les résultats de livraison incertains bloquent un renvoi.
Les logs contiennent les identifiants concernés et le type d'erreur, sans token,
URL de callback ou réponse API.

Les annonces sont réservées en SQLite par date, membre et compte, avec un nonce
stable et l'ID du message reçu. En cas d'arrêt ou de résultat réseau incertain,
le bot recherche le contenu enregistré parmi ses propres messages dans
l'historique du salon. Si le résultat reste inconnu, l'annonce devient
`uncertain` et **n'est pas renvoyée automatiquement**, pour éviter un second ping.
Un redémarrage permet une nouvelle recherche après correction des permissions.
Ne pas vider les tables anniversaire ou restaurer une ancienne base pendant
que les annonces sont actives : cela perdrait leur protection contre les doublons.
Une seule instance du bot doit utiliser cette base.

## Bascule sur Unraid

1. Conserver `BIRTHDAY_CHANNEL_ID=0` dans le `.env` privé et déployer après fusion :

   ```bash
   bash /mnt/user/appdata/discord-wot-verify/scripts/update-unraid.sh
   ```

   Le script sauvegarde la base avant migration. L'image installe aussi `tzdata`
   pour rendre le fuseau disponible indépendamment du système hôte.

2. Laisser le bot remplir les dates, puis lire les compteurs sans modifier SQLite :

   ```bash
   docker exec wot-verify python -c 'import sqlite3; db=sqlite3.connect("file:/data/verify.sqlite3?mode=ro", uri=True); print("Dates connues / liaisons :", db.execute("SELECT count(b.created_at), count(*) FROM links l LEFT JOIN account_birthdays b ON b.region=l.region AND b.account_id=l.account_id").fetchone())'
   docker logs --tail=100 wot-verify
   ```

   Si `DATABASE_PATH` diffère de `/data/verify.sqlite3`, adapter ce chemin.
   Examiner les dates encore inconnues et les avertissements WG ; une panne
   permet une reprise quinze minutes plus tard. Ne jamais renseigner une date
   estimée pour terminer le remplissage.

3. Dans Discord, autoriser le bot à **voir le salon**, **lire l'historique** et
   **envoyer des messages** dans le salon général `525037740690112527` du serveur
   configuré. Vérifier **Gérer les pseudos** et placer le rôle du bot au-dessus
   des membres à renommer. Le propriétaire et les membres au-dessus du bot
   peuvent rester impossibles à renommer ; leurs anniversaires restent annoncés.

4. Arrêter **le traitement anniversaire de l'ancien bot**, y compris sa
   planification et ses annonces, avant l'activation. Il peut rester installé
   pendant la recette, mais ne doit plus ajouter de bougies ni envoyer de ping.

5. Modifier uniquement cette ligne du `.env` privé, puis recréer le conteneur :

   ```dotenv
   BIRTHDAY_CHANNEL_ID=525037740690112527
   ```

   ```bash
   bash /mnt/user/appdata/discord-wot-verify/scripts/update-unraid.sh
   docker logs --tail=100 wot-verify
   ```

   Une activation après 7 h traite le jour courant au premier passage. Les
   bougies de l'ancien bot sont normalisées sans cumul ; les anciennes bougies
   expirées sont retirées. Aucun nouveau `/setup` n'est nécessaire.

6. Suivre deux journées de traitement : nombre exact de bougies, ping limité aux
   membres concernés, conservation des tags et retrait le lendemain à 7 h.
   Vérifier aussi `/resync`, une relance et un membre non modifiable. Un jour sans
   anniversaire ne doit produire aucune annonce. Retirer définitivement l'ancien
   bot après cette recette ; ne pas falsifier les dates WG en production pour
   provoquer un anniversaire.

Pour suspendre la fonction, remettre `BIRTHDAY_CHANNEL_ID=0` et recréer le
conteneur. Cela suspend les annonces et modifications périodiques ; les données
et nettoyages en attente restent conservés pour la prochaine activation.
La collecte des dates continue. Pour examiner une annonce incertaine :

```bash
docker exec wot-verify python -c 'import sqlite3; db=sqlite3.connect("file:/data/verify.sqlite3?mode=ro", uri=True); print(db.execute("SELECT id, day, channel_id, state, message_id FROM birthday_announcements WHERE state IN (?, ?)", ("sending", "uncertain")).fetchall())'
```

La recette réelle et l'observation des deux journées restent à réaliser sur le
serveur ; les tests automatisés utilisent uniquement SQLite temporaire et des
doubles WG/Discord. Voir [l'issue #32](https://github.com/Rayman223/discord-wot-verify/issues/32).
