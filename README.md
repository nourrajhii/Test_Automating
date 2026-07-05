# Test Automating

Agent IA de test automatisé : donnez un code HTML/JS/JSX, il génère et exécute des scénarios de test Selenium pour toutes les fonctionnalités de l'interface.

```
HTML/JS/JSX → Analyse UI → Scénarios de test → Scripts Selenium → Exécution → Rapport
```

## Overview

Un LLM local (Ollama) analyse le code, détecte les éléments interactifs, comprend les fonctionnalités réelles de la page, génère un scénario de test par fonctionnalité, écrit le script Selenium correspondant et l'exécute. Résultat diffusé en temps réel via SSE.

## Features

- Analyse HTML statique + fallback LLM (JSX/React)
- Filtrage des éléments cachés/dupliqués
- Génération de scénarios de test en 2 étapes (fonctionnalités → scénarios)
- Génération automatique de scripts Selenium robustes
- Exécution isolée avec rapport détaillé (succès/échec, logs)
- Streaming temps réel (SSE)
- 100% local

## Tech Stack

**Frontend** : React, Vite, EventSource (SSE)

**Backend** : FastAPI, Pydantic, BeautifulSoup4, httpx, Selenium, webdriver-manager, Uvicorn, Ollama

## Architecture

```
test-auto/
├── app/
│   ├── models/
│   │   └── schemas.py                # Schémas Pydantic
│   ├── services/
│   │   ├── html_parser_service.py    # Analyse HTML → éléments UI
│   │   ├── scenario_service.py       # Génération des scénarios (LLM)
│   │   ├── script_service.py         # Génération des scripts Selenium
│   │   ├── executor_service.py       # Exécution des scripts
│   │   └── html_server_service.py    # Serveur HTML éphémère
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


