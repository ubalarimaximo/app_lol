"""
Genera Maxinualete.exe en la carpeta dist/
Uso: python build.py
"""
import subprocess
import sys
import os
from pathlib import Path


def png_to_ico(png_path: Path, ico_path: Path):
    from PIL import Image
    src = Image.open(png_path).convert("RGBA")
    imgs = [src.resize((s, s), Image.LANCZOS) for s in (256, 128, 64, 48, 32, 16)]
    imgs[0].save(ico_path, format="ICO", append_images=imgs[1:])


def main():
    # Instala PyInstaller si no está
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "pyinstaller", "-q"],
        check=True,
    )

    # Ruta al paquete customtkinter (necesario para incluir sus temas)
    try:
        import customtkinter as ctk
        ctk_path = os.path.dirname(ctk.__file__)
    except ImportError:
        print("ERROR: customtkinter no está instalado. Ejecutá: pip install customtkinter")
        sys.exit(1)

    app_dir = Path(os.path.abspath(__file__)).parent

    # Convertir icono PNG → ICO para el .exe
    png_icon = app_dir / "assets" / "icons" / "app_icon.png"
    ico_icon = app_dir / "assets" / "icons" / "app_icon.ico"
    if png_icon.exists():
        print("Convirtiendo icono PNG -> ICO...")
        png_to_ico(png_icon, ico_icon)
    else:
        print("AVISO: no se encontró assets/icons/app_icon.png, el .exe usará icono por defecto.")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",                          # un solo .exe
        "--windowed",                         # sin consola negra
        "--name", "Maxinualete",
        "--add-data", f"{ctk_path}{os.pathsep}customtkinter",
        "--add-data", f"{app_dir / 'assets'}{os.pathsep}assets",
    ]

    if ico_icon.exists():
        cmd += ["--icon", str(ico_icon)]

    cmd.append(str(app_dir / "main.py"))

    print("Construyendo .exe (puede tardar 1-2 minutos)...\n")
    subprocess.run(cmd, check=True, cwd=app_dir)

    exe_path = app_dir / "dist" / "Maxinualete.exe"
    if exe_path.exists():
        print(f"\nOK! Ejecutable generado en:\n  {exe_path}")
    else:
        print("\nBuild completado. Revisá la carpeta dist\\")


if __name__ == "__main__":
    main()
