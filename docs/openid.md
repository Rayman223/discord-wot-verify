# Connexion Wargaming OpenID directe

La connexion utilise OpenID 2.0 directement, à la manière de Battle-Conquest. Le bot demande uniquement une preuve d'identité : il ne demande plus d'`access_token` et n'appelle plus `auth/login` ou `auth/prolongate`. `WG_APP_ID` reste nécessaire pour l'API publique `account/info`, utilisée pour récupérer les pseudos après vérification et lors des synchronisations.

## Vérification

Le bouton crée un état signé valable dix minutes, lié au membre et au salon Discord. `/login` le valide et pose un cookie sécurisé avant de rediriger vers `https://wargaming.net/id/openid/`, endpoint annoncé dans le document XRDS de `https://wargaming.net/id/`. Le fournisseur est fixé dans le code ; aucune URL reçue du navigateur ne sert de destination HTTP de vérification.

Au retour sur `/callback`, le serveur vérifie le cookie, consomme l'état à usage unique, puis contrôle : namespace OpenID 2.0, mode `id_res`, endpoint exact, URL `return_to` exacte avec l'état signé, identité WG identique au `claimed_id`, région explicite compatible avec la configuration, champs obligatoires couverts par la signature et nonce daté à moins de cinq minutes. Les paramètres dupliqués sont refusés.

La signature est vérifiée par POST `check_authentication` vers l'endpoint WG fixé, via HTTPS et sans suivre de redirection. La réponse doit annoncer le namespace OpenID et `is_valid:true` ; elle est limitée à 4 Kio. Les erreurs réseau et les assertions invalides affichent une erreur sans journaliser les paramètres sensibles.

Le nonce du fournisseur est conservé dans SQLite avec contrainte d'unicité : une assertion déjà acceptée est refusée, y compris après redémarrage ou avec un nouvel état Discord. Les nonces de plus de vingt minutes sont purgés à la prochaine vérification réussie, au-delà de la fenêtre où ils pourraient encore être acceptés. Le pseudo vient de l'API publique et non d'un paramètre libre du callback.

La région initiale reste EU. Pour une identité WG sans sous-domaine régional, la région configurée demandée par `game_realm` est utilisée, comme dans Battle-Conquest. Les autres régions restent à valider avec leurs comptes réels. Le bot ne suit aucune découverte arbitraire vers des fournisseurs tiers ; un changement officiel d'endpoint WG nécessiterait une mise à jour du code.

## Déployer la migration

Après fusion, lancer le script Unraid habituel depuis User Scripts :

```bash
#!/bin/bash
exec /bin/bash /mnt/user/appdata/discord-wot-verify/scripts/update-unraid.sh
```

La migration idempotente `002_openid_nonces.sql` s'applique automatiquement au démarrage et ajoute uniquement la table de nonces et son index. Les liaisons existantes, le rôle joueur, les pseudos et le panneau Discord sont conservés. Aucun changement de `.env`, de domaine ou de configuration SWAG n'est nécessaire. Les anciens callbacks avec jeton ne sont plus acceptés : relancer le bouton pour toute connexion commencée avant la mise à jour.

Vérifier `curl --fail https://discord-auth.battle-conquest.eu/healthz`, puis utiliser le bouton Discord existant avec un membre ordinaire. Après connexion WG, contrôler la confirmation, le rôle, le pseudo et le bouton **Return to Discord**. Tester aussi l'annulation chez WG et le refus d'un callback rejoué. Un refus de renommage pour hiérarchie conserve la liaison et reste présenté comme un résultat partiel.

Les tests automatiques utilisent des bases SQLite temporaires et des réponses HTTP simulées. Ils ne remplacent pas la recette avec un compte WG réel. Les URL de callback restent sensibles même sans jeton : ne pas les partager et conserver la désactivation des logs HTTP du vhost.

Référence : [OpenID Authentication 2.0, vérification des assertions](https://openid.net/specs/openid-authentication-2_0.html#verification).
