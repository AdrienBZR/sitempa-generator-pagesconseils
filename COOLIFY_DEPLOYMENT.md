# Procédure de Déploiement Coolify

Ce microservice utilise une variable d'environnement pour s'authentifier auprès de Google, ce qui évite de stocker le fichier `.json` dans le code.

## 1. Préparer les Credentials

Il faut transformer le contenu de votre fichier JSON de compte de service en une **seule ligne de texte** (chaîne JSON) pour la mettre dans une variable d'environnement.

### Méthode via Terminal (Mac/Linux)
Ouvrez votre terminal dans le dossier du projet et lancez :

```bash
# Affiche le contenu en une seule ligne sans espaces inutiles
cat ../creds/solocal-poc-f9a485d4ac05.json | jq -c .
```

*Si vous n'avez pas `jq`, vous pouvez juste copier tout le contenu du fichier et utiliser un outil en ligne "Minify JSON" ou simplement le copier tel quel (Coolify gère généralement les sauts de ligne, mais le minifier est plus sûr).*

## 2. Configurer Coolify

1.  Allez sur votre instance **Coolify**.
2.  Créez une nouvelle ressource (Application) à partir de votre dépôt GitHub (`sitempa-generator-pagesconseils`).
3.  Dans la configuration de l'application, allez dans **Environment Variables** (Secrets).
4.  Ajoutez une nouvelle variable :
    *   **Key** : `GOOGLE_CREDENTIALS_JSON`
    *   **Value** : Collez ici le contenu complet du fichier JSON (copié à l'étape 1).
5.  Sauvegardez.

## 2.1 Configurer le Port (CRITIQUE)

Par défaut, Coolify peut écouter sur le port 3000 ou 80. Votre application écoute sur le port **8000**.

1.  Allez dans l'onglet **Configuration** > **General**.
2.  Dans le champ **Ports Exposes**, mettez : `8000`.
3.  Sauvegardez.
4.  Cliquez sur **Redeploy**.

## 3. Déploiement

1.  Lancez le déploiement.
2.  Coolify va :
    *   Cloner le repo.
    *   Lire le `Dockerfile`.
    *   Construire l'image (installation de python, fastapi, etc.).
    *   Lancer le conteneur.

## 4. Test

Une fois déployé, votre sitemap sera accessible sur :
`https://<votre-domaine>/sitemap.xml`
