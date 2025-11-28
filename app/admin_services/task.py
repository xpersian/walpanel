from app.admin_services.api import PanelAPI
from app.oprations.panel import panel_operations
from app.oprations.admin import admin_operations
from app.schema._input import CreateUserInput, UpdateUserInput
from app.log.logger_config import logger
from fastapi.responses import JSONResponse
from fastapi import status
from datetime import datetime
from uuid import uuid4
import string
import secrets
import json


class Task:
    def get_sublinks(self, db, username):
        try:
            admin = admin_operations.get_admin_data(db, username)
            panel = panel_operations.panel_data(db, admin.panel_id)
            return {"result": f"https://{panel.sub}/"}
        except Exception as e:
            logger.error(f"Error getting sublinks: {e}")
            return JSONResponse(
                content={"error": str(e)},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def check_admin_traffic(self, db, username, _traffic):
        try:
            admin = admin_operations.get_admin_data(db, username)
            if admin.traffic < _traffic:
                return False
            return True
        except Exception as e:
            return False

    def reduce_admin_traffic(self, db, username, _traffic):
        try:
            admin_operations.reduce_traffic(db, username, _traffic)
        except Exception as e:
            return False

    def get_users(self, db, username):
        client_list = []
        try:
            admin = admin_operations.get_admin_data(db, username)
            panel = panel_operations.panel_data(db, admin.panel_id)
            panel_api = PanelAPI(panel.url, panel.username, panel.password)

            all_inbounds = panel_api.get_all_inbounds()
            inbounds_list = all_inbounds.get("obj", [])

            inbound = next(
                (i for i in inbounds_list if i.get("id") == admin.inbound_id), None
            )
            if not inbound:
                logger.warning(f"Inbound {admin.inbound_id} not found.")
                return {"clients": []}

            client_stats = inbound.get("clientStats", [])
            settings = inbound.get("settings")
            clients_info = []
            if settings:
                try:
                    settings_json = (
                        json.loads(settings) if isinstance(settings, str) else settings
                    )
                    clients_info = settings_json.get("clients", [])
                except Exception as e:
                    logger.warning(f"Failed to parse settings: {e}")

            online_users = panel_api.online_users()
            if online_users is None:
                online_users = []

            for stat in client_stats:
                info = next(
                    (c for c in clients_info if c.get("email") == stat.get("email")),
                    {},
                )
                total_usage = (stat.get("up", 0) + stat.get("down", 0)) / (1024**3)
                client_online = stat.get("email") in online_users

                client_list.append(
                    {
                        "email": stat.get("email"),
                        "online": client_online,
                        "id": info.get("id"),
                        "totalGB": stat.get("total", 0) / (1024**3),
                        "totalUsage": total_usage,
                        "expiryTime": datetime.fromtimestamp(
                            stat.get("expiryTime", 0) / 1000
                        ).strftime("%Y-%m-%d"),
                        "enable": stat.get("enable", False),
                        "subId": info.get("subId", None),
                    }
                )
        except Exception as e:
            logger.error(f"Error fetching user list: {e}")
            return {
                "clients": client_list,
                "error": "Failed to fetch user list, try again.",
            }
        return {"clients": client_list}

    async def total_users_in_inbound(self, db, username: str, retry: int = 0) -> int:
        client_count = 0
        admin = admin_operations.get_admin_data(db, username)
        panel = panel_operations.panel_data(db, admin.panel_id)
        try:
            panel_api = PanelAPI(panel.url, panel.username, panel.password)
            result = panel_api.show_users(admin.inbound_id)
            client_count = len(result)
            return client_count
        except Exception as e:
            logger.error(f"fetching user list: {e} and returned 0")
            return client_count

    def create_user(self, db, username: str, request: CreateUserInput):
        if not self.check_admin_traffic(db, username, request.totalGB):
            return JSONResponse(
                content={"error": "Traffic limit reached"},
                status_code=status.HTTP_403_FORBIDDEN,
            )
        try:
            admin = admin_operations.get_admin_data(db, username)
            panel = panel_operations.panel_data(db, admin.panel_id)
            panel_api = PanelAPI(panel.url, panel.username, panel.password)

            _uuid = str(uuid4())
            subid = generate_secure_random_text(16)

            result = panel_api.add_user(
                admin.inbound_id,
                _uuid,
                subid,
                request.email,
                int(request.totalGB * (1024**3)),
                request.expiryTime,
                admin.inbound_flow,
            )
            if result:
                _traffic = round(request.totalGB, 1)
                self.reduce_admin_traffic(db, username, _traffic)
        except Exception as e:
            return JSONResponse(
                content={"error": f"Create user failed: {str(e)}"},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def delete_client(self, db, username: str, user_id: str, name: str):
        try:
            admin = admin_operations.get_admin_data(db, username)
            panel = panel_operations.panel_data(db, admin.panel_id)
            panel_api = PanelAPI(panel.url, panel.username, panel.password)

            result = panel_api.delete_client(
                admin.inbound_id,
                user_id,
            )
        except Exception as e:
            return JSONResponse(
                content={"error": f"Delete client failed: {str(e)}"},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def update_client(self, db, username: str, user_id: str, request: UpdateUserInput):
        try:
            admin = admin_operations.get_admin_data(db, username)
            panel = panel_operations.panel_data(db, admin.panel_id)
            panel_api = PanelAPI(panel.url, panel.username, panel.password)

            client = panel_api.get_user(request.email)
            if not client:
                return JSONResponse(
                    content={"error": f"User with email {request.email} not found."},
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            old_total_gb = client.get("total", 0) / (1024**3)
            used_gb = (client.get("up", 0) + client.get("down", 0)) / (1024**3)
            remaining_gb = max(0, round(old_total_gb - used_gb, 1))
            net_traffic_cost = request.totalGB - remaining_gb

            if net_traffic_cost > 0 and not self.check_admin_traffic(
                db, username, net_traffic_cost
            ):
                return JSONResponse(
                    content={
                        "error": "Traffic limit reached. You don't have enough traffic for this update."
                    },
                    status_code=status.HTTP_403_FORBIDDEN,
                )

            result = panel_api.update_client(
                admin.inbound_id,
                user_id,
                request.email,
                int(request.totalGB * (1024**3)),
                request.expiryTime,
                admin.inbound_flow,
                request.subid,
            )
            if result:
                panel_api.reset_traffic(admin.inbound_id, request.email)
                if net_traffic_cost != 0:
                    self.reduce_admin_traffic(db, username, net_traffic_cost)

            return JSONResponse(content=result, status_code=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Update client failed for user {request.email}: {e}")
            return JSONResponse(
                content={"error": f"Update client failed: {str(e)}"},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def reset_client_traffic(self, db, username: str, email: str):
        try:
            admin = admin_operations.get_admin_data(db, username)
            panel = panel_operations.panel_data(db, admin.panel_id)
            panel_api = PanelAPI(panel.url, panel.username, panel.password)

            client = panel_api.get_user(email)
            used_traffic = (client.get("up", 0) + client.get("down", 0)) / (1024**3)
            _traffic_to_charge = round(used_traffic, 1)

            if not self.check_admin_traffic(db, username, _traffic_to_charge):
                return JSONResponse(
                    content={"error": "Your traffic is not enough to reset this user."},
                    status_code=status.HTTP_403_FORBIDDEN,
                )

            result = panel_api.reset_traffic(
                admin.inbound_id,
                email,
            )
            if result and admin.return_traffic:
                self.reduce_admin_traffic(db, username, _traffic_to_charge)

            return JSONResponse(content=result, status_code=status.HTTP_200_OK)
        except Exception as e:
            return JSONResponse(
                content={"error": f"Reset traffic failed: {str(e)}"},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


admin_task = Task()


def generate_secure_random_text(length=16):
    characters = string.ascii_letters + string.digits
    return "".join(secrets.choice(characters) for _ in range(length))
