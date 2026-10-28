# Contacts de clan

Le panneau `/setup` comporte un second bouton **Become a clan contact**. Le membre doit avoir lié son compte via Wargaming OpenID. Le bot interroge `clans/accountinfo` sur la région configurée et accepte exactement les grades `commander`, `executive_officer` et `recruitment_officer`. Les grades sont vérifiés côté serveur à chaque demande, sans faire confiance à une information Discord ou à un paramètre fourni par le joueur.

Une contrainte SQLite garantit un seul contact par couple région/clan, et un seul clan par contact. Si la place appartient déjà à un autre membre, le bouton répond en privé : **This clan already has a contact. Please ask a moderator for a manual verification.** Aucun remplacement ni message automatique à un modérateur n'est effectué.

Après attribution, le pseudo devient **`PseudoWG [TAG]`**. Le tag et le pseudo viennent de l'API WG. Le bot refuse un résultat dépassant 32 caractères plutôt que tronquer le pseudo. `/resync` et la synchronisation périodique préservent ce suffixe pour les contacts enregistrés.

Si le renommage échoue après attribution du rôle, le rôle et la réservation sont conservés ; le membre reçoit une explication et peut réessayer le bouton ou `/resync`. Un refus explicite de l'attribution du rôle libère une nouvelle réservation ; en cas d'erreur réseau au résultat incertain, elle est conservée pour éviter un doublon et permettre une vérification manuelle. Une panne WG ne provoque aucune attribution ni suppression.

## Configurer sur Unraid

1. Créer ou retrouver le rôle Discord **contact de clan**, distinct de **joueur** et sans permission administrateur. Copier son ID avec le mode développeur. Placer le rôle du bot au-dessus de celui-ci et des membres à renommer.
2. Ajouter dans le `.env` privé :

   ```dotenv
   CLAN_CONTACT_ROLE_ID=ID_DU_ROLE_CONTACT
   ```

   Remplacer `ID_DU_ROLE_CONTACT` par l'identifiant numérique. La valeur `0` (valeur par défaut) désactive l'attribution, avec un message explicite au clic ; aucun identifiant réel n'est nécessaire pour livrer le code.
3. Après fusion, lancer le User Script habituel :

   ```bash
   exec /bin/bash /mnt/user/appdata/discord-wot-verify/scripts/update-unraid.sh
   ```

   La migration idempotente `003_clan_contacts.sql` crée la table et les contraintes d'unicité au démarrage. Les liaisons existantes sont conservées. Aucun changement Nginx ou application WG n'est requis.
4. Exécuter `/setup` dans le salon pour publier un panneau avec les deux boutons. Les anciens messages ne sont pas modifiés automatiquement ; supprimer l'ancien panneau si nécessaire. Le nouveau bouton reste fonctionnel après redémarrage.
5. Tester avec un membre éligible sous le rôle du bot : attribution, pseudo `PseudoWG [TAG]`, puis `/resync`. Tester un membre non lié, un grade non éligible et un second candidat du même clan.

## Vérification manuelle et retrait

Un modérateur disposant de **Manage Roles** peut utiliser :

```text
/admin_contact_remove discord_id:IDENTIFIANT_DISCORD
```

La commande vérifie les permissions à l'exécution, retire le rôle contact puis libère la place. Le pseudo actuel et la liaison Wargaming sont conservés. Si le membre est parti ou banni, sa place peut aussi être libérée. En cas de refus du retrait par Discord, la réservation est conservée pour réessayer. Une nouvelle personne peut ensuite cliquer sur le bouton pour faire vérifier son propre compte.

`/unlink` et `/admin_unlink` retirent également le rôle contact avant de supprimer la liaison. Un échec de retrait conserve la liaison et la réservation. Ces retraits explicites font partie de la suppression demandée par l'utilisateur ou l'administrateur.

La révocation automatique après départ du serveur ou changement de clan/grade n'est pas activée : cette règle reste à décider. Si un contact change de clan ou de grade, une tentative de bouton ou de synchronisation demande une vérification manuelle sans transférer la place ni supprimer automatiquement le rôle. Après un départ, la réservation reste occupée jusqu'au retrait manuel ; elle n'attribue pas automatiquement le rôle contact au retour du membre. Les attributions de rôles faites manuellement en dehors du bot ne créent aucune réservation en base et doivent être contrôlées par les modérateurs.

Recette suivie dans [l'issue #5](https://github.com/Rayman223/discord-wot-verify/issues/5). Les tests automatiques utilisent des bases temporaires et des doubles Discord/WG ; la recette réelle nécessite l'ID du rôle et des comptes de clan.
