# profiles/

Ce dossier contient les profils utilisateurs et les CV. **Il n'est pas versionné**
(voir `.gitignore`) : il contient des données personnelles réelles.

Seuls deux fichiers sont suivis par git :

- `example.json` — un profil entièrement fictif, qui sert de documentation du schéma.
- `README.md` — ce fichier.

## Contenu attendu

```
profiles/
  gabriel.json      # non versionné
  camila.json       # non versionné
  example.json      # versionné, fictif
  cv/
    gabriel.txt     # non versionné
    camila.txt      # non versionné
```

## Régénérer les profils

Les deux profils réels sont produits à partir des notebooks d'origine par
l'adaptateur, qui lit les valeurs au lieu de les retaper :

```bash
python3 tools/build_profiles.py
```

## Vérifier qu'aucune valeur n'a été perdue

```bash
python3 tests/test_profile_completeness.py
```

Le test compare chaque profil aux valeurs lues **en direct** dans
`job_scout.ipynb` et `job_scout_camila/job_scout_camila.ipynb`. Code de sortie 0
= rien n'a été perdu.

## Le schéma

Défini dans `jobscout/profile.py` (Pydantic v2). Documenté dans
`ARCHITECTURE.md` section 2.
