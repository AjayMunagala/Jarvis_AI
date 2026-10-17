"""Construction of the v5.1-v5.6 advanced intelligence layer.

Kept in one module so ``jarvis.main`` changes by a few lines only. The router
wraps the provider JARVIS already built; nothing in the existing chat path is
replaced. PostgreSQL stores are used when persistence is enabled, otherwise
in-memory stores labelled non-durable.
"""

import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from jarvis.contextual.updates import MemoryUpdateService
from jarvis.intelligence.mock_provider import MockProvider
from jarvis.knowledge_graph.service import GraphService
from jarvis.knowledge_graph.stores import MemoryGraphStore, PostgresGraphStore
from jarvis.multimodal.service import MultimodalService
from jarvis.personalization.service import PersonalizationService
from jarvis.personalization.stores import MemoryPreferenceStore, PostgresPreferenceStore
from jarvis.routing.evaluation import EvaluationHarness
from jarvis.routing.models import EvaluationReport
from jarvis.routing.registry import ModelRegistry, load_policies
from jarvis.routing.router import ModelRouter

MAX_STORED_REPORTS = 20


@dataclass
class IntelligenceLayer:
    registry: ModelRegistry
    router: ModelRouter
    harness: EvaluationHarness
    graph: GraphService
    memory_updates: MemoryUpdateService
    multimodal: MultimodalService
    personalization: PersonalizationService
    reports: "OrderedDict[str, tuple[object, EvaluationReport]]" = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def store_report(self, owner_id, report: EvaluationReport) -> None:
        with self._lock:
            self.reports[report.report_id] = (owner_id, report)
            while len(self.reports) > MAX_STORED_REPORTS:
                self.reports.popitem(last=False)

    def report(self, owner_id, report_id: str) -> EvaluationReport | None:
        with self._lock:
            entry = self.reports.get(report_id)
        return entry[1] if entry and entry[0] == owner_id else None


def build_intelligence(settings, provider) -> IntelligenceLayer:
    registry = ModelRegistry.load(openai_model=getattr(settings, "openai_model", None))
    adapters = {"mock": MockProvider()}
    if provider is not None and getattr(provider, "name", None):
        adapters[provider.name] = provider
    router = ModelRouter(registry, load_policies(), adapters)
    if settings.persistence_enabled:
        url = settings.persistence_database_url.get_secret_value()
        timeouts = dict(connect_timeout_seconds=settings.persistence_connect_timeout_seconds,
                        operation_timeout_seconds=settings.persistence_operation_timeout_seconds)
        graph_store, preference_store = PostgresGraphStore(url, **timeouts), PostgresPreferenceStore(url, **timeouts)
    else:
        graph_store, preference_store = MemoryGraphStore(), MemoryPreferenceStore()
    return IntelligenceLayer(
        registry=registry, router=router, harness=EvaluationHarness(registry, adapters),
        graph=GraphService(graph_store), memory_updates=MemoryUpdateService(),
        # No real multimodal adapter exists yet; media requests report "unsupported".
        multimodal=MultimodalService(router, {}), personalization=PersonalizationService(preference_store),
    )
