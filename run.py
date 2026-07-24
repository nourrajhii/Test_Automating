"""
run.py
------
Lance le serveur FastAPI avec uvicorn.
A utiliser a la place de la commande uvicorn CLI pour eviter le probleme
de WatchFiles qui redemarrait le serveur chaque fois qu'un script Selenium
etait ecrit dans reports/.

Usage :
    python run.py

Equivalent CLI (si vous preferez) :
    uvicorn app.main:app --reload --reload-exclude "reports" --reload-exclude "uploads"
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload= False,
        reload_excludes=["reports", "uploads", "*.pyc", "__pycache__"],
    )