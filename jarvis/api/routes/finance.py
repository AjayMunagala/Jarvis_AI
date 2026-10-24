"""Authenticated personal finance and analytical intelligence API routes (v3.7).

Strict safety invariant: ZERO trading, broker, or order execution APIs.
Purely analytical record keeping, net worth monitoring, and cash flow forecasting.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.finance.models import (
    AccountCreateRequest,
    FinanceForecast,
    FinancialAccount,
    FinancialAccountPage,
    FinancialAnalyticsReport,
    FinancialImportBatchPage,
    FinancialSnapshot,
    FinancialTransaction,
    FinancialTransactionPage,
    ImportCommitRequest,
    ImportCommitResult,
    ImportPreview,
    ImportPreviewRequest,
    ImportUndoResult,
    ForecastAccuracyReport,
    ForecastSnapshot,
    ForecastSnapshotCreateRequest,
    ForecastSnapshotPage,
    TransactionCreateRequest,
)
from jarvis.finance.importing import (
    ImportAccountNotFound,
    ImportBalanceOutOfRange,
    ImportPreviewNotFound,
    ImportRefused,
)
from jarvis.finance.service import FinanceService
from jarvis.finance.tracking import ForecastSnapshotExists
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability

router = APIRouter(prefix="/finance", tags=["finance"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return owner_id


def _service(request: Request) -> FinanceService:
    service = getattr(request.app.state, "finance_service", None)
    if not isinstance(service, FinanceService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The finance service is unavailable.",
        )
    return service


# ============================================================= Accounts

@router.post(
    "/accounts",
    response_model=FinancialAccount,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def create_account(
    request: Request,
    body: AccountCreateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> FinancialAccount:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_WRITE)
    return await run_in_threadpool(_service(request).create_account, owner, body)


@router.get("/accounts", response_model=FinancialAccountPage, responses=_ERRORS)
async def list_accounts(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> FinancialAccountPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_READ)
    return await run_in_threadpool(
        _service(request).list_accounts, owner, limit=limit, offset=offset
    )


@router.get("/accounts/{account_id}", response_model=FinancialAccount, responses=_ERRORS)
async def get_account(
    request: Request,
    account_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> FinancialAccount:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_READ)
    acc = await run_in_threadpool(_service(request).get_account, owner, account_id)
    if acc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested financial account was not found.",
        )
    return acc


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_account(
    request: Request,
    account_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Response:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_WRITE)
    deleted = await run_in_threadpool(_service(request).delete_account, owner, account_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested financial account was not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================= Transactions

@router.post(
    "/transactions",
    response_model=FinancialTransaction,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def record_transaction(
    request: Request,
    body: TransactionCreateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> FinancialTransaction:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_WRITE)
    try:
        return await run_in_threadpool(_service(request).record_transaction, owner, body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.get("/transactions", response_model=FinancialTransactionPage, responses=_ERRORS)
async def list_transactions(
    request: Request,
    account_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> FinancialTransactionPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_READ)
    return await run_in_threadpool(
        _service(request).list_transactions,
        owner,
        account_id=account_id,
        limit=limit,
        offset=offset,
    )


@router.delete("/transactions/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_transaction(
    request: Request,
    transaction_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Response:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_WRITE)
    deleted = await run_in_threadpool(
        _service(request).delete_transaction, owner, transaction_id
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested financial transaction was not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================= Analytics

@router.post(
    "/analytics/snapshot",
    response_model=FinancialSnapshot,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def generate_snapshot(
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> FinancialSnapshot:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_WRITE)
    return await run_in_threadpool(_service(request).generate_snapshot, owner)


@router.get("/analytics/snapshot", response_model=FinancialSnapshot, responses=_ERRORS)
async def get_latest_snapshot(
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> FinancialSnapshot:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_READ)
    snap = await run_in_threadpool(_service(request).get_latest_snapshot, owner)
    if snap is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No financial snapshot has been generated yet.",
        )
    return snap


@router.get("/analytics/report", response_model=FinancialAnalyticsReport, responses=_ERRORS)
async def get_analytics_report(
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> FinancialAnalyticsReport:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_READ)
    return await run_in_threadpool(_service(request).get_analytics_report, owner)


# ============================================================= Forecasts (v6.6, analysis only)

@router.get("/forecast", response_model=FinanceForecast, responses=_ERRORS)
async def get_forecast(
    request: Request,
    horizon: int = Query(default=3, ge=1, le=12),
    min_train: int = Query(default=6, ge=3, le=24),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> FinanceForecast:
    """Backtested monthly cash-flow forecast with uncertainty. Analysis only; never executes anything."""
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_READ)
    return await run_in_threadpool(
        _service(request).get_forecast, owner, horizon=horizon, min_train=min_train
    )



# ============================================================= Forecast snapshots and accuracy (v7.2)
# A monthly snapshot is not scheduled automatically in this version: the owner calls POST /forecast/snapshots
# (at most one per UTC day unless force=true). TODO: a scheduler job type could call it later.

_SNAPSHOT_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 503)}


@router.post("/forecast/snapshots", response_model=ForecastSnapshot, status_code=status.HTTP_201_CREATED,
             responses=_SNAPSHOT_ERRORS)
async def create_forecast_snapshot(
    request: Request,
    body: ForecastSnapshotCreateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ForecastSnapshot:
    """Compute the forecast exactly like GET /forecast and store it, timestamped and immutable. Analysis only."""
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_WRITE)
    try:
        return await run_in_threadpool(_service(request).create_forecast_snapshot, owner, body)
    except ForecastSnapshotExists as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A forecast snapshot was already taken today (UTC): {exc.existing_id}. "
                   "Send force=true to take another.",
        ) from exc


@router.get("/forecast/snapshots", response_model=ForecastSnapshotPage, responses=_ERRORS)
async def list_forecast_snapshots(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ForecastSnapshotPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_READ)
    return await run_in_threadpool(_service(request).list_forecast_snapshots, owner, limit=limit, offset=offset)


@router.get("/forecast/snapshots/{snapshot_id}", response_model=ForecastSnapshot, responses=_ERRORS)
async def get_forecast_snapshot(
    request: Request,
    snapshot_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ForecastSnapshot:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_READ)
    snapshot = await run_in_threadpool(_service(request).get_forecast_snapshot, owner, snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="The requested forecast snapshot was not found.")
    return snapshot


@router.delete("/forecast/snapshots/{snapshot_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_forecast_snapshot(
    request: Request,
    snapshot_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Response:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_WRITE)
    if not await run_in_threadpool(_service(request).delete_forecast_snapshot, owner, snapshot_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="The requested forecast snapshot was not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/forecast/accuracy", response_model=ForecastAccuracyReport, responses=_ERRORS)
async def get_forecast_accuracy(
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ForecastAccuracyReport:
    """Accuracy of stored forecasts for months that are complete, against current data. Analysis only."""
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_READ)
    return await run_in_threadpool(_service(request).get_forecast_accuracy, owner)


# ============================================================= Bank CSV imports (v7.1)

_IMPORT_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 413, 422, 503)}


def _balance_out_of_range() -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                         detail="The resulting account balance would be out of range; nothing was changed.")


def _account_missing() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                         detail="The requested financial account was not found.")


@router.post("/imports/preview", response_model=ImportPreview, status_code=status.HTTP_201_CREATED,
             responses=_IMPORT_ERRORS)
async def preview_import(
    request: Request,
    body: ImportPreviewRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ImportPreview:
    """Parse and check a bank CSV file. Nothing is stored except an in-memory preview that expires in 30 minutes."""
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_WRITE)
    try:
        return await run_in_threadpool(_service(request).preview_import, owner, body)
    except ImportAccountNotFound as exc:
        raise _account_missing() from exc
    except ImportRefused as exc:  # content-free messages by construction
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/imports/{preview_id}/commit", response_model=ImportCommitResult, responses=_IMPORT_ERRORS)
async def commit_import(
    request: Request,
    preview_id: UUID,
    body: ImportCommitRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ImportCommitResult:
    """Store the preview's valid rows with the balance change, all or nothing. A preview commits once."""
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_WRITE)
    try:
        return await run_in_threadpool(_service(request).commit_import, owner, preview_id, body)
    except ImportPreviewNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="The import preview was not found or has expired.") from exc
    except ImportAccountNotFound as exc:
        raise _account_missing() from exc
    except ImportBalanceOutOfRange as exc:
        raise _balance_out_of_range() from exc


@router.get("/imports", response_model=FinancialImportBatchPage, responses=_ERRORS)
async def list_imports(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> FinancialImportBatchPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_READ)
    return await run_in_threadpool(_service(request).list_import_batches, owner, limit=limit, offset=offset)


@router.delete("/imports/{batch_id}", response_model=ImportUndoResult, responses=_IMPORT_ERRORS)
async def undo_import(
    request: Request,
    batch_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ImportUndoResult:
    """Undo an import: remove exactly that batch's transactions and reverse their balance effect."""
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.FINANCE_WRITE)
    try:
        result = await run_in_threadpool(_service(request).undo_import, owner, batch_id)
    except ImportBalanceOutOfRange as exc:
        raise _balance_out_of_range() from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="The requested import batch was not found.")
    return result
