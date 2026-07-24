# Test Automating

Agent IA de test automatisé : donnez un code HTML/JS/JSX, il génère et
exécute des scénarios de test Selenium pour toutes les fonctionnalités
de l'interface.

```
HTML/JS/JSX → Analyse UI → Scénarios de test → Scripts Selenium → Exécution → Rapport
```

## Overview

Un LLM local (Ollama) analyse le code, détecte les éléments interactifs,
comprend les fonctionnalités réelles de la page, génère un scénario de
test par fonctionnalité, écrit le script Selenium correspondant et
l'exécute. Résultat diffusé en temps réel via SSE.

## Features

**Analyse UI**
- Analyse HTML statique + fallback LLM (JSX/React)
- Filtrage des éléments cachés/dupliqués, des liens "junk" (skip links, submits redondants)
- Détection de zone fonctionnelle par élément (barre de nav, pied de page, formulaires, auth/oauth, zones de contenu…)
- Titre de zone basé sur le contenu réel (h1 → h2 → h3 → legend → aria-label → label → texte affiché), jamais une classe CSS ou un ID
- Détection des sous-menus au survol (hover/dropdown) et du sélecteur de langue (regroupé en un seul scénario)
- Détection des liens externes
- Mode "URL réelle" : rendu de la page dans Chrome headless (JS exécuté, utile pour les SPA React/Vue) plutôt qu'une vision IA qui peut halluciner des éléments inexistants
- Serveur HTTP éphémère pour servir le HTML brut fourni par l'utilisateur, afin que Selenium puisse l'ouvrir via une vraie URL

**Compréhension de l'application**
- Résumé compact de toute l'interface (jamais tronqué) envoyé au LLM en un seul appel
- Détection du type d'application, de son but, et du rôle de chaque zone
- Détection des capacités métier typiques de ce type d'application (référentiel pour la suite)
- Regroupement des features en parcours utilisateur cohérents (ex. connexion → tableau de bord)

**Génération de scénarios**
- Détection déterministe (sans LLM) des zones structurelles/contenu avec règles de test connues d'avance (navigation, pied de page, actualités, téléchargements, galerie, réseaux sociaux…)
- Génération de scénarios de test en 2 étapes (fonctionnalités → scénarios)
- Scénarios toujours dédiés pour les fonctionnalités importantes : Connexion, Inscription, Mot de passe oublié, Sélection de la langue
- Scénarios enrichis (objectif, préconditions, étapes, résultat attendu), pas seulement une liste d'actions
- Titres de scénarios systématiquement fonctionnels, jamais un identifiant technique brut

**Scripts Selenium & exécution**
- Résolution de chaque étape texte du scénario vers l'élément UI réel qu'elle désigne (par similarité de texte, avec seuil de confiance)
- Génération automatique de scripts Selenium robustes, avec repli XPath par texte visible si le sélecteur CSS est instable
- Exécution isolée en parallèle (plusieurs navigateurs Chrome headless simultanés)
- Retry automatique en cas d'échec (déterministe, et variante pilotée par un agent LLM qui décide de réessayer ou d'abandonner)

**Rapport**
- Rapport HTML autonome (un seul fichier, screenshots encodés en base64)
- Détail par scénario : succès/échec, logs, code Selenium généré, temps d'exécution, étapes/assertions passées
- Diagnostic intelligent des échecs (analyse déterministe des logs) + suggestion de correctif (patch de code avant/après)
- Streaming temps réel (SSE) de toutes les étapes du pipeline
- Rapport qui démarre directement sur les scénarios, sans information technique avant
- 100% local

## Tech Stack

**Frontend** : React, Vite, EventSource (SSE)
**Backend** : FastAPI, Pydantic, BeautifulSoup4, httpx, Selenium, webdriver-manager, Uvicorn, Ollama, LangGraph

## Architecture

```
test-auto/
├── app/
│   ├── models/
│   │   └── schemas.py                # Schémas Pydantic
│   ├── services/                     # Agents spécialisés du pipeline
│   │                                  # (voir "Pipeline en détail" ci-dessous)
│   ├── config.py
│   └── main.py                       # API + pipeline SSE
├── frontend/
│   └── src/
│       ├── App.jsx
│       └── main.jsx
├── reports/                          # Scripts générés (runtime)
├── uploads/                          # Fichiers uploadés (runtime)
├── run.py
└── requirements.txt
```

## Pipeline en détail

Le pipeline n'est pas une chaîne linéaire à 4 étapes mais un graphe
**LangGraph** orchestrant plusieurs agents spécialisés, chacun avec un
rôle précis — certains appellent le LLM, d'autres sont 100% déterministes
(plus rapides, jamais tronqués, résultats garantis) :

```
HTML/JS/JSX
   │
   ▼
Analyse UI ──────────────────────────────────────────────
   │  Parsing HTML (+ fallback LLM pour JSX/React), détection
   │  des éléments interactifs, de leur zone fonctionnelle
   │  (barre de nav, pied de page, sous-menus, formulaires,
   │  zones de contenu...), des liens de langue, des menus
   │  au survol.
   ▼
Compréhension de l'application ─────────────────────────
   │  Un résumé compact de toute l'interface est construit,
   │  puis analysé par le LLM en 1 seul appel pour déterminer
   │  le TYPE d'application, son BUT, et les CAPACITÉS métier
   │  qu'elle offre typiquement — sert de référentiel pour
   │  éviter que chaque lot de features soit deviné dans le
   │  vide.
   ▼
Détection déterministe des zones structurelles/contenu ──
   │  Navigation, pied de page, sous-menus, sélecteur de
   │  langue, actualités, téléchargements, galerie, réseaux
   │  sociaux... : ces zones ont des règles de test connues
   │  d'avance et reçoivent directement un scénario, SANS
   │  passer par le LLM.
   ▼
Extraction de fonctionnalités (LLM, par lot) ───────────
   │  Ce qui n'a pas déjà été couvert ci-dessus (formulaires
   │  spécifiques, fonctionnalités métier propres à la page)
   │  est envoyé au LLM par lots pour identifier les features
   │  restantes. Connexion / Inscription / Mot de passe
   │  oublié / Sélection de la langue ont toujours leur
   │  propre scénario dédié.
   ▼
Regroupement en parcours utilisateur ────────────────────
   │  Les features qui forment ensemble un parcours logique
   │  (ex. "Se connecter" → "Accéder au tableau de bord") sont
   │  regroupées en un seul appel LLM.
   ▼
Plan de test + Génération des scénarios ─────────────────
   │  Construction du plan de test puis génération des
   │  scénarios finaux (titre fonctionnel, objectif,
   │  préconditions, étapes, résultat attendu).
   ▼
Génération des scripts Selenium ─────────────────────────
   │  Chaque step texte du scénario est d'abord "résolu" vers
   │  l'élément UI réel qu'il désigne (par similarité de
   │  texte), puis traduit en code Selenium.
   ▼
Exécution (Chrome headless, en parallèle) ───────────────
   │  En cas d'échec : retry avec fallback XPath par texte
   │  visible, jusqu'à une limite de tentatives.
   ▼
Rapport ──────────────────────────────────────────────────
      Rapport HTML autonome : synthèse, détail par scénario
      (steps, code Selenium, logs, screenshot), diagnostic
      intelligent des échecs + suggestion de correctif.
```

Le rapport final commence toujours directement sur les scénarios : aucune
information technique (classes CSS, IDs, noms de zones internes) n'est
affichée avant. Les titres de scénarios sont systématiquement des phrases
fonctionnelles ("Navigation via la barre de navigation", "Consultation
des actualités", "Sélection de la langue"...), jamais un identifiant
technique brut.

## Getting Started

**Prérequis** : Python 3.10+, Node.js 18+, Chrome, Ollama

```bash
ollama pull llama3.2:3b && ollama serve
```

**Backend**

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python run.py
```

**Frontend**

```bash
cd frontend
npm install
npm run dev
```

> Ne pas lancer `uvicorn --reload` directement — utiliser `run.py` (exclut `reports/` du watcher).
