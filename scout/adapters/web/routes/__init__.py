"""Web 路由组 mixin（W1-W4 全量拆分完成，2026-09-14）."""

from scout.adapters.web.routes.auth import AuthRoutes
from scout.adapters.web.routes.a2a import A2aRoutes
from scout.adapters.web.routes.voice import VoiceRoutes
from scout.adapters.web.routes.channels import ChannelRoutes
from scout.adapters.web.routes.automation import AutomationRoutes
from scout.adapters.web.routes.sessions import SessionRoutes
from scout.adapters.web.routes.memory import MemoryRoutes
from scout.adapters.web.routes.knowledge import KnowledgeRoutes
from scout.adapters.web.routes.goals import GoalRoutes
from scout.adapters.web.routes.config import ConfigRoutes
from scout.adapters.web.routes.skills import SkillRoutes
from scout.adapters.web.routes.observability import ObservabilityRoutes
from scout.adapters.web.routes.integrations import IntegrationRoutes
from scout.adapters.web.routes.chat import ChatRoutes
from scout.adapters.web.routes.ws import WsRoutes

__all__ = ['AuthRoutes', 'A2aRoutes', 'VoiceRoutes', 'ChannelRoutes', 'AutomationRoutes', 'SessionRoutes', 'MemoryRoutes', 'KnowledgeRoutes', 'GoalRoutes', 'ConfigRoutes', 'SkillRoutes', 'ObservabilityRoutes', 'IntegrationRoutes', 'ChatRoutes', 'WsRoutes']
