# Career Compass Setup

Prompt pour Lovable — Module 1 : Onboarding

Copie-colle ceci dans Lovable pour démarrer le projet.

Je veux construire le module d'onboarding d'une application de recherche d'emploi assistée par IA. Ce module est le point d'entrée : inscription, upload de CV, quelques questions rapides pour cerner ce que l'utilisateur recherche.

Ce que je veux dans cette première version

1. Authentification

Connexion via Google (Supabase Auth avec provider Google).

Après connexion, créer automatiquement une ligne dans une table profiles liée à l'utilisateur (user_id = Supabase Auth user id).

2. Upload de CV

Un écran qui permet d'uploader un fichier CV (PDF ou texte).

Le fichier doit être stocké dans Supabase Storage, dans un bucket dédié (par exemple resumes), avec un chemin qui isole les fichiers par utilisateur ({user_id}/cv.pdf ou équivalent), et une politique de sécurité (RLS / storage policy) qui empêche un utilisateur d'accéder aux fichiers d'un autre.

Une fois l'upload terminé, enregistrer le chemin du fichier dans la table profiles (colonne cv_storage_path ou équivalent). Pas besoin d'extraire le texte du CV à ce stade, juste de le stocker proprement.

3. Questions d'onboarding Après l'upload, poser ces questions dans un flux simple (une à la fois ou un petit formulaire, à toi de proposer le meilleur enchaînement UX) :

Question A : "As-tu un ou plusieurs postes précis en tête, ou préfères-tu qu'on explore large à partir de ton CV ?" — deux choix : "J'ai des postes précis en tête" / "Je préfère explorer largement".

Question B (affichée seulement si "postes précis" est choisi) : "Quels intitulés de poste recherches-tu ?" — champ texte libre, qui accepte plusieurs entrées (l'utilisateur doit pouvoir ajouter 2-3 intitulés, pas juste un seul champ texte).

Question C (toujours affichée) : "Où cherches-tu ?" — une ville/région (texte libre ou autocomplete si simple à faire) + un choix parmi "À distance", "Hybride", "Sur site", "Peu importe" (choix multiple possible).

Stocke les réponses dans la table profiles :

search_mode : "precise" ou "exploration"

target_titles : tableau de texte (vide si mode exploration)

target_location : texte

work_types : tableau de texte parmi ["remote", "hybrid", "onsite"]

Contraintes importantes

Ne construis PAS encore d'écran affichant des résultats d'offres d'emploi. Ce module s'arrête après la question C, avec un écran de confirmation simple type "Merci, on prépare ta recherche" ou équivalent. Le matching et l'affichage des offres viendront dans un module séparé, pas encore prêt.

Structure la table profiles en gardant en tête qu'elle va évoluer pour accueillir des données de scoring plus riches plus tard (dimensions pondérées, exclusions, etc.) — donc privilégie une structure simple et extensible (une colonne raw_answers en jsonb en plus des colonnes explicites ci-dessus n'est pas une mauvaise idée, pour ne rien perdre si on ajoute des questions plus tard).

Active Row Level Security sur profiles : chaque utilisateur ne doit voir/modifier que sa propre ligne.

Reste simple sur le design pour cette première itération : un flux clair, pas besoin de polish visuel poussé pour l'instant.

Ce que je ferai après (pas à construire maintenant, juste pour contexte)

Ce module va être connecté à un moteur de scraping/scoring développé séparément en Python, qui va lire ces données de profil pour chercher et évaluer des offres d'emploi. Les résultats reviendront dans des tables séparées (job_results, applications) qu'on construira plus tard. Pas besoin d'anticiper leur structure maintenant, juste garder profiles propre et pas surchargée.

This project was built with [Lovable](https://lovable.dev).

## Build with Lovable

Continue developing this project in the [Lovable editor](https://lovable.dev/projects/932434d1-f765-4be8-a763-24175ab20d98).

- **Ship faster**: describe what you want to build and Lovable handles the code.
- **Stay in sync**: every change made in Lovable is committed straight to this repository.
- **Full ownership**: this code is yours. Push to `main` on GitHub and your changes sync back into Lovable, ready for your next prompt.

## Development

Prefer working locally? You need Node.js and npm — [install with nvm](https://github.com/nvm-sh/nvm#installing-and-updating).

```sh
git clone <this-repository-url>
cd <repository-name>
npm i
npm run dev
```
