import os
import requests

TAIGA_URL = os.getenv("TAIGA_URL", "https://api.taiga.io/api/v1")
TAIGA_USER = os.getenv("TAIGA_USER", "")
TAIGA_PASS = os.getenv("TAIGA_PASS", "")
TAIGA_PROJECT = os.getenv("TAIGA_PROJECT", "eduardogr92-pagos")


class TaigaClient:

    def __init__(self):
        self.base_url = os.getenv("TAIGA_URL", "https://api.taiga.io/api/v1")
        self.token = None

    def login_taiga(self):
        response = requests.post(
            f"{self.base_url}/auth",
            json={
                "type": "normal",
                "username": TAIGA_USER,
                "password": TAIGA_PASS

            })
        data=response.json()
        self.token = data["auth_token"]
        return self.token

    def _headers(self):
        if not self.token:
            self.login_taiga()

        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

    def get_project(self):
        response = requests.get(f"{self.base_url}/projects/by_slug",
                                params={
                                    "slug": TAIGA_PROJECT
                                },
                                headers=self._headers()
                                )
        return response.json()

    def get_user_story(self, hu_ref: int):
        """
        Obtiene una Historia de Usuario por su número de referencia (HU-X)
        
        Args:
            hu_ref: Número de referencia de la HU (ej: 1, 2, 3...)
        
        Returns:
            dict: Información completa de la Historia de Usuario
        """
        project = self.get_project()
        response = requests.get(f"{self.base_url}/userstories/by_ref",
                                params={
                                    "project": project["id"],
                                    "ref": hu_ref
                                },
                                headers=self._headers(),
                                timeout=30)

        story_id = response.json()["id"]
        return self.get_user_story_by_id(story_id)

    def get_user_story_by_id(self, story_id: int):
        """Obtiene el detalle completo de una HU por su id interno de Taiga."""
        response = requests.get(f"{self.base_url}/userstories/{story_id}",
                                headers=self._headers(),
                                timeout=30)
        return response.json()


    def get_active_sprint(self, project_id: int):
        """Devuelve el milestone (sprint) activo del proyecto, o None si no hay ninguno."""
        response = requests.get(
            f"{self.base_url}/milestones",
            params={"project": project_id, "closed": "false"},
            headers=self._headers(),
            timeout=30
        )
        milestones = response.json()
        if not isinstance(milestones, list) or not milestones:
            return None
        # El sprint activo es el que tiene estimated_finish más próximo y no está cerrado
        milestones_abiertos = [m for m in milestones if not m.get("closed", False)]
        if not milestones_abiertos:
            return None
        return sorted(milestones_abiertos, key=lambda m: m.get("estimated_finish", ""))[0]

    def get_user_stories_by_sprint(self, milestone_id: int, project_id: int):
        """Devuelve solo las user stories (no tareas ni issues) del sprint indicado."""
        response = requests.get(
            f"{self.base_url}/userstories",
            params={"project": project_id, "milestone": milestone_id},
            headers=self._headers(),
            timeout=30
        )
        return response.json()

    def get_all_user_stories(self):
        project = self.get_project()
        response = requests.get(f"{self.base_url}/userstories",
                                params={
                                    "project": project["id"],

                                },
                                headers=self._headers(),
                                timeout=30)
        return response.json()
