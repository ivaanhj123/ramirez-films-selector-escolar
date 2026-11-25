# PickFlow — Selector de Fotos Escolares

Primer prototipo para Windows. El programa analiza fotos localmente y copia el resultado a dos carpetas, sin borrar los originales:

- `Seleccionadas`: fotos con resolución y nitidez aceptables.
- `Revision`: fotos con posible desenfoque, resolución baja, duplicados o errores de lectura.

Las detecciones de ojos se muestran como aviso, pero no descartan por sí solas una foto: en sesiones escolares los rostros pueden ser pequeños, estar de perfil o usar lentes.

Por defecto está activa la opción **Dejar solo una mejor foto por serie**. La aplicación agrupa tomas consecutivas visualmente parecidas y conserva la de mayor puntuación en `Seleccionadas`; las demás quedan en `Revision`. Puedes desactivar esa casilla si quieres conservar varias fotos buenas.

## Cómo probarlo

1. Instala Python 3.11 o posterior para Windows.
2. Abre PowerShell en esta carpeta.
3. Ejecuta:

```powershell
python -m pip install -r requirements_selector.txt
python selector_fotos_escolares.py
```

4. Elige la carpeta de fotos escolares y una carpeta de salida distinta.
5. Pulsa **Analizar y separar fotos**.

La app incluye subcarpetas por defecto y conserva la estructura original dentro del resultado. La agrupación por alumno cuando las fotos están completamente mezcladas será una siguiente mejora, porque conviene ajustarla con ejemplos reales de tus sesiones escolares.

## Actualizaciones automáticas

La aplicación revisa la versión automáticamente al abrirse y también incluye el botón **Buscar actualizaciones**. Si encuentra una versión nueva, descarga el ZIP desde GitHub, reemplaza los archivos de la aplicación y se reinicia.

El archivo `version.json` usa este formato:

```json
{
  "version": "0.5.0",
  "download_url": "https://github.com/ivaanhj123/ramirez-films-selector-escolar/raw/refs/heads/main/Ramirez_Films_Selector_Escolar.zip",
  "notes": "Actualización automática y una mejor foto por serie"
}
```

