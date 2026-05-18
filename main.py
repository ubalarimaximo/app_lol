import sys
import os

# Agrega la carpeta raíz al path para que "src" sea importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.app import LoLAssistantApp

if __name__ == "__main__":
    app = LoLAssistantApp()
    app.run()
