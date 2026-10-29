# Logo WoT Verify

[Télécharger le logo PNG](wot-verify-avatar.png) (1254 × 1254 pixels, environ 1,4 Mo).

Le char rappelle World of Tanks ; la coche verte représente la vérification du compte. Le fond bleu nuit et les contours dorés gardent un contraste marqué. Le dessin sans texte reste utilisable en français et en anglais, avec une marge pour le recadrage circulaire de Discord. Il s’agit d’un emblème communautaire indépendant.

## Installer l’avatar

1. Télécharger le fichier PNG original, sans faire de capture d’écran.
2. Ouvrir le [portail développeur Discord](https://discord.com/developers/applications), puis l’application **WoT Verify** (`1565004673352474717`).
3. Dans **Bot**, remplacer l’avatar par ce PNG, conserver le cadrage centré complet et enregistrer les modifications.
4. Dans **General Information**, utiliser également le PNG pour **App Icon**, puis enregistrer.
5. Vérifier l’avatar du bot dans le serveur et dans le salon **#verification**, y compris en petit format. Discord peut mettre un peu de temps à actualiser son cache.

Cette opération se fait dans Discord ; aucune variable d’environnement ni reconstruction du conteneur n’est nécessaire. L’ajout du fichier au dépôt ne change pas automatiquement l’avatar distant.

## Création

Image créée avec l’outil intégré `image_gen`, sans retouche du fichier généré. Le logo est distribué sous la licence MIT du dépôt.

Prompt utilisé :

```text
Use case: logo-brand. Create the final square Discord bot avatar for an independent community bot named WoT Verify that verifies World of Tanks player identities. A distinctive, simple flat vector-style emblem: a bold ivory front-facing tank silhouette with a single short barrel and clearly readable tracks, paired with a prominent bright emerald verification check integrated at its lower right. Deep navy circular medallion against a matching solid deep navy square background. Restrained warm gold accent around the medallion. Strong balanced geometry, thick crisp outlines, premium gaming utility identity, excellent contrast and legible at 32px. All meaningful shapes within the central 75 percent of the square so a circular Discord crop preserves the design. One icon only, centered, no contact sheet, no mockup, no words, no letters, no watermark, no tiny details, no copied World of Tanks, Wargaming or Discord official logos. Finished production avatar, preferably 1024x1024.
```
