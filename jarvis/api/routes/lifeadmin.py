"""Authenticated life administration API routes (v7.5).

Nothing here pays a bill, moves money, signs in to a bank or provider, stores a document file or looks anything
up. "Paid" records what the owner says happened. The prefix is ``/lifeadmin`` (not ``/admin``) so it cannot be
mistaken for, or clash with, an administrative surface.
"""

from typing import Any, Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.lifeadmin.models import (
    BillCreateRequest, BillPage, BillUpdateRequest, BillView, DocumentCreateRequest, DocumentPage,
    DocumentUpdateRequest, DocumentView, OverdueResult, PaidRequest, PaidResult, PaymentPage, ReminderResult,
    UpcomingResult,
)
from jarvis.lifeadmin.service import (
    LifeAdminConflict, LifeAdminInvalid, LifeAdminNotFound, LifeAdminService, LifeAdminUnavailable,
)
from jarvis.lifeadmin.stores import LifeAdminStorageError
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability

router = APIRouter(prefix="/lifeadmin", tags=["lifeadmin"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)}
_WRITE_ERRORS = {**_ERRORS, 409: {"model": ErrorResponse}}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="A valid bearer credential is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    return owner_id


def _service(request: Request) -> LifeAdminService:
    service = getattr(request.app.state, "lifeadmin_service", None)
    if not isinstance(service, LifeAdminService):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="The life administration service is unavailable.")
    return service


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, LifeAdminNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, LifeAdminInvalid):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, LifeAdminConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, LifeAdminUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=503, detail="Life administration storage is unavailable.")


_HANDLED = (LifeAdminNotFound, LifeAdminInvalid, LifeAdminConflict, LifeAdminUnavailable, LifeAdminStorageError)


async def _call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return await run_in_threadpool(function, *args, **kwargs)
    except _HANDLED as exc:
        raise _translate(exc) from exc


def _authorize(request: Request, owner_id: UUID | None, capability: Capability) -> tuple[UUID, LifeAdminService]:
    owner = _owner(owner_id)
    enforce_capability(request, owner, capability)
    return owner, _service(request)


# ============================================================= documents

@router.post("/documents", response_model=DocumentView, status_code=status.HTTP_201_CREATED,
             responses=_WRITE_ERRORS)
async def create_document(request: Request, body: DocumentCreateRequest,
                          owner_id: UUID | None = Depends(authenticated_owner)) -> DocumentView:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_WRITE)
    return await _call(service.create_document, owner, body)


@router.get("/documents", response_model=DocumentPage, responses=_ERRORS)
async def list_documents(request: Request,
                         status_filter: Literal["active", "renewed", "expired", "archived"] | None = Query(
                             default=None, alias="status"),
                         limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                         owner_id: UUID | None = Depends(authenticated_owner)) -> DocumentPage:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_READ)
    return await _call(service.list_documents, owner, status=status_filter, limit=limit, offset=offset)


@router.get("/documents/{document_id}", response_model=DocumentView, responses=_ERRORS)
async def get_document(request: Request, document_id: UUID,
                       owner_id: UUID | None = Depends(authenticated_owner)) -> DocumentView:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_READ)
    return await _call(service.get_document, owner, document_id)


@router.patch("/documents/{document_id}", response_model=DocumentView, responses=_ERRORS)
async def update_document(request: Request, document_id: UUID, body: DocumentUpdateRequest,
                          owner_id: UUID | None = Depends(authenticated_owner)) -> DocumentView:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_WRITE)
    return await _call(service.update_document, owner, document_id, body)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_document(request: Request, document_id: UUID,
                          owner_id: UUID | None = Depends(authenticated_owner)) -> Response:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_WRITE)
    await _call(service.delete_document, owner, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================= bills

@router.post("/bills", response_model=BillView, status_code=status.HTTP_201_CREATED, responses=_WRITE_ERRORS)
async def create_bill(request: Request, body: BillCreateRequest,
                      owner_id: UUID | None = Depends(authenticated_owner)) -> BillView:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_WRITE)
    return await _call(service.create_bill, owner, body)


@router.get("/bills", response_model=BillPage, responses=_ERRORS)
async def list_bills(request: Request,
                     status_filter: Literal["active", "archived"] | None = Query(default=None, alias="status"),
                     limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                     owner_id: UUID | None = Depends(authenticated_owner)) -> BillPage:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_READ)
    return await _call(service.list_bills, owner, status=status_filter, limit=limit, offset=offset)


@router.get("/bills/{bill_id}", response_model=BillView, responses=_ERRORS)
async def get_bill(request: Request, bill_id: UUID,
                   owner_id: UUID | None = Depends(authenticated_owner)) -> BillView:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_READ)
    return await _call(service.get_bill, owner, bill_id)


@router.patch("/bills/{bill_id}", response_model=BillView, responses=_ERRORS)
async def update_bill(request: Request, bill_id: UUID, body: BillUpdateRequest,
                      owner_id: UUID | None = Depends(authenticated_owner)) -> BillView:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_WRITE)
    return await _call(service.update_bill, owner, bill_id, body)


@router.delete("/bills/{bill_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_bill(request: Request, bill_id: UUID,
                      owner_id: UUID | None = Depends(authenticated_owner)) -> Response:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_WRITE)
    await _call(service.delete_bill, owner, bill_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/bills/{bill_id}/paid", response_model=PaidResult, responses=_WRITE_ERRORS)
async def mark_paid(request: Request, bill_id: UUID, body: PaidRequest | None = Body(default=None),
                    owner_id: UUID | None = Depends(authenticated_owner)) -> PaidResult:
    """Record a payment you made elsewhere and advance the next due date. JARVIS does not pay anything."""
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_WRITE)
    return await _call(service.mark_paid, owner, bill_id, body)


@router.get("/bills/{bill_id}/payments", response_model=PaymentPage, responses=_ERRORS)
async def list_payments(request: Request, bill_id: UUID, limit: int = Query(default=50, ge=1, le=200),
                        offset: int = Query(default=0, ge=0),
                        owner_id: UUID | None = Depends(authenticated_owner)) -> PaymentPage:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_READ)
    return await _call(service.list_payments, owner, bill_id, limit=limit, offset=offset)


# ============================================================= views and reminders

@router.get("/upcoming", response_model=UpcomingResult, responses=_ERRORS)
async def upcoming(request: Request, days: int = Query(default=30, ge=1, le=366),
                   owner_id: UUID | None = Depends(authenticated_owner)) -> UpcomingResult:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_READ)
    return await _call(service.upcoming, owner, days)


@router.get("/overdue", response_model=OverdueResult, responses=_ERRORS)
async def overdue(request: Request, owner_id: UUID | None = Depends(authenticated_owner)) -> OverdueResult:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_READ)
    return await _call(service.overdue, owner)


@router.post("/reminders/sync", response_model=ReminderResult, responses=_ERRORS)
async def sync_reminders(request: Request, owner_id: UUID | None = Depends(authenticated_owner)) -> ReminderResult:
    owner, service = _authorize(request, owner_id, Capability.LIFEADMIN_WRITE)
    return await _call(service.sync_reminders, owner)
