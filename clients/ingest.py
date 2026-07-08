from clients.taiga_connect import TaigaClient
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


def extract_info_allHU():
    try:
        taiga = TaigaClient()
        project = taiga.get_project()
        project_id = project["id"]

        sprint = taiga.get_active_sprint(project_id)
        if not sprint:
            logger.warning("No hay sprint activo en el proyecto")
            return []

        logger.info(f"Sprint activo encontrado: '{sprint.get('name')}' (id={sprint['id']})")
        hus = taiga.get_user_stories_by_sprint(sprint["id"], project_id)

        if not isinstance(hus, list) or not hus:
            logger.warning("No se encontraron HUs en el sprint activo")
            return []

        # El listado no incluye 'description'; se enriquece con el detalle de cada HU
        hus_detalle = []
        for hu in hus:
            try:
                logger.info(f"Obteniendo HU #{hu.get('ref')}...")
                hus_detalle.append(taiga.get_user_story_by_id(hu["id"]))
            except Exception as e:
                logger.warning(
                    f"No se pudo obtener el detalle de la HU id={hu.get('id')}: {e}; "
                    f"se usa la version del listado"
                )
                hus_detalle.append(hu)

        info_hus = extract_characteristic_us(hus_detalle)
        logger.info(f"Se extrajeron {len(info_hus)} HUs del sprint activo")
        return info_hus
    except Exception as e:
        logger.error(f"Error al obtener HUs del sprint activo: {str(e)}")
        raise


def _limpiar_texto(texto):
    """Normaliza un texto: elimina marcas Markdown, saltos de linea, espacios no
    separables (\\xa0) y otros caracteres de control, dejando texto plano con los
    espacios colapsados en uno solo."""
    if not texto:
        return texto
    # Normaliza unicode (NBSP y similares) a su forma compatible
    texto = unicodedata.normalize("NFKC", texto)
    # Elimina marcas Markdown habituales
    texto = re.sub(r"`+", "", texto)               # codigo inline `...`
    texto = re.sub(r"\*\*|__", "", texto)           # negrita **..** o __..__
    texto = re.sub(r"(?<!\w)[*_](?!\s)", "", texto) # cursiva *..* o _.._
    texto = re.sub(r"^\s{0,3}#{1,6}\s*", "", texto, flags=re.MULTILINE)  # encabezados #
    texto = re.sub(r"^\s*[-*+]\s+", "", texto, flags=re.MULTILINE)        # vinetas de lista
    # Sustituye cualquier secuencia de espacios en blanco (incluye \n, \t, \xa0) por un espacio
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


def extract_characteristic_us(list_us: list[dict]):
    """
    Normaliza una lista de Historias de Usuario extrayendo características clave
    
    Args:
        list_us: Lista de diccionarios HU
    
    Returns:
        list: Lista de HU normalizadas con campos: id, ref, subject, description, status
    """
    us_normalized = []

    for hu in list_us:
        us_normalized.append({
            "id": hu.get("id"),
            "ref": hu.get("ref"),
            "subject": _limpiar_texto(hu.get("subject")),
            "description": _limpiar_texto(hu.get("description")),
            "status": (hu.get("status_extra_info") or {}).get("name", hu.get("status"))
        })

    return us_normalized


def extract_info(hu_number: int = 1):
    """
    Extrae información de una Historia de Usuario específica por su número
    
    Args:
        hu_number: Número de referencia de la HU (ej: 1, 2, 3...)
    
    Returns:
        list: Lista con la HU normalizada
        
    Raises:
        ValueError: Si el número de HU no es válido
        Exception: Si hay error al conectar con Taiga
    """
    try:
        if not isinstance(hu_number, int) or hu_number < 1:
            raise ValueError(f"El número de HU debe ser un entero positivo, recibido: {hu_number}")
        
        logger.info(f"Obteniendo HU #{hu_number}...")
        taiga = TaigaClient()
        hu = taiga.get_user_story(hu_number)
        
        if not hu:
            logger.warning(f"No se encontró la HU #{hu_number}")
            return []
        
        listHU = [hu]
        normalized_hus = extract_characteristic_us(listHU)
        logger.info(f"HU #{hu_number} extraída correctamente")
        return normalized_hus
    except ValueError as e:
        logger.error(f"Error de validación: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Error al obtener la HU #{hu_number}: {str(e)}")
        raise


if __name__ == '__main__':
    import sys
    try:
        hu_number = int(sys.argv[1]) if len(sys.argv) > 1 else 1
        resultado = extract_info(hu_number)
        print(f"\nHU #{hu_number}:")
        for hu in resultado:
            print(f"  - HU-{hu['ref']} +' '+ {hu['subject']}")
            print(f"  - Descripción: {hu['description']}")
            print()
    except ValueError as e:
        logger.error(f"Argumento inválido: {str(e)}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error en la ejecución: {str(e)}")
        sys.exit(1)
