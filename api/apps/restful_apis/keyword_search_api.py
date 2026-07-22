"""API-token-only internal keyword search contract."""

from quart import request

from api.apps import AUTH_API, login_required
from api.apps.services.corpus_delegation_service import CorpusDelegationError, CorpusDelegationService
from api.apps.services.keyword_search_service import KeywordSearchService, SearchUnavailableError
from api.utils.api_utils import add_tenant_id_to_kwargs, get_error_data_result, get_result


@manager.route("/datasets/keyword-search", methods=["POST"])  # noqa: F821
@login_required(auth_types=AUTH_API)
@add_tenant_id_to_kwargs
async def keyword_search(tenant_id: str):
    try:
        payload = await request.get_json() or {}
        if not isinstance(payload, dict) or set(payload) - {"dataset_ids", "document_ids", "query", "match", "exclude", "offset", "limit", "highlight", "count_only"}:
            raise ValueError("invalid keyword search request")
        delegation = CorpusDelegationService.verify(request.headers.get("X-RAGFlow-Delegation"), tenant_id)
        dataset_ids = payload.get("dataset_ids")
        if not isinstance(dataset_ids, list) or not set(dataset_ids).issubset(delegation.dataset_ids):
            raise PermissionError("dataset not delegated")
        result = KeywordSearchService().search_authorized(
            delegation.actor_id,
            dataset_ids,
            payload.get("document_ids"),
            payload.get("query", ""),
            match=payload.get("match", "any"),
            exclude=payload.get("exclude"),
            offset=payload.get("offset", 0),
            limit=payload.get("limit", 20),
            highlight=payload.get("highlight", True),
            count_only=payload.get("count_only", False),
        )
        return get_result(data=result)
    except (PermissionError, CorpusDelegationError):
        response = get_error_data_result(message="NOT_FOUND")
        response.status_code = 404
        return response
    except SearchUnavailableError:
        response = get_error_data_result(message="SEARCH_UNAVAILABLE")
        response.status_code = 503
        return response
    except (TypeError, ValueError):
        response = get_error_data_result(message="INVALID_ARGUMENT")
        response.status_code = 400
        return response
