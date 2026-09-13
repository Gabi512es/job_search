# legacy/

Les deux notebooks **d'origine**, figés au bloc 6.

```
legacy/job_scout_original.ipynb
legacy/job_scout_camila_original.ipynb
```

## Pourquoi ils sont là

Quatre suites de tests comparent le nouveau code au comportement d'origine en
**lisant ces notebooks** plutôt qu'en recopiant leurs valeurs :

- `tests/test_profile_completeness.py` — les profils n'ont rien perdu
- `tests/test_filters.py` — exclusions et déduplication identiques
- `tests/test_store_export.py` — colonnes et largeurs d'export identiques
- `tools/build_profiles.py` — regénère les profils depuis les notebooks

Au bloc 6 les notebooks de la racine sont devenus de simples lanceurs. Sans
cette copie figée, ces tests auraient perdu leur référence sans bruit.

## Pourquoi ils ne sont pas versionnés

Ils contiennent encore le nom du candidat dans le prompt de lettre de
motivation (`COVER_LETTER_SYSTEM`). Seul ce README est suivi par git.

Ne pas les modifier : ce sont des témoins, pas du code vivant.
