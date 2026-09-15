"""Measurement state machine with explicit lifecycle and confirmation workflow."""
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, FiniteFloat

from database import Database


class MeasurementState(str, Enum):
    """Measurement lifecycle states."""
    DRAFT = 'draft'  # Initial OCR results, pending review
    NEEDS_CORRECTION = 'needs_correction'  # User flagged for correction
    PENDING_CONFIRMATION = 'pending_confirmation'  # Ready for final confirmation
    CONFIRMED = 'confirmed'  # Production record created
    REJECTED = 'rejected'  # Explicitly rejected
    CANCELLED = 'cancelled'  # Cancelled before completion


class FieldValue(BaseModel):
    """Single field measurement value."""
    field_id: str
    instrument_id: uuid.UUID
    name: str
    value: FiniteFloat
    unit: str = Field(min_length=1, max_length=20)
    range_min: Optional[float] = None
    range_max: Optional[float] = None
    quality_check_passed: bool = True


class MeasurementRevision(BaseModel):
    """A revision of measurement values."""
    revision: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=100)
    timestamp: str
    reason: str = Field(min_length=1, max_length=1000)
    fields: list[FieldValue]
    experiment_context_ref: Optional[str] = None


class MeasurementConfirmation(BaseModel):
    """Final confirmation or rejection."""
    confirmation_id: str
    measurement_id: str
    revision: int
    actor: str
    action: str  # 'confirm' or 'reject'
    reason: Optional[str]
    confirmed_at: str
    fields_snapshot: Optional[list[FieldValue]] = None


class MeasurementStateMachine:
    """
    Manage measurement lifecycle with explicit state transitions.

    State flow:
    DRAFT -> NEEDS_CORRECTION -> PENDING_CONFIRMATION -> CONFIRMED
                                                       -> REJECTED
                              -> CANCELLED
    """

    def __init__(self, db: Database):
        self.db = db

    def create_measurement(self, measurement_id: str, burst_id: Optional[str],
                          camera_id: str, operator: str,
                          job_ids: list[str], context: dict) -> dict:
        """
        Create a new measurement in DRAFT state.

        Args:
            measurement_id: Unique measurement ID
            burst_id: Optional burst/连拍 ID
            camera_id: Camera identifier
            operator: Operator name
            job_ids: Associated OCR job IDs
            context: Measurement context (instruments, expected photos, etc.)

        Returns:
            Created measurement document
        """
        with self.db.transaction('IMMEDIATE') as conn:
            now = datetime.now(timezone.utc).isoformat()

            measurement = {
                'id': measurement_id,
                'burst_id': burst_id,
                'camera_id': camera_id,
                'operator': operator,
                'job_ids': job_ids,
                'context': context,
                'state': MeasurementState.DRAFT.value,
                'revision': 1,
                'created_at': now,
                'updated_at': now,
                'fields': [],
                'revision_history': []
            }

            # Insert into photo_measurements table
            burst_key = burst_id or measurement_id
            conn.execute('''
                INSERT INTO photo_measurements(id, camera, burst_key, status, document)
                VALUES(?,?,?,?,?)
            ''', (measurement_id, camera_id, burst_key,
                  MeasurementState.DRAFT.value, json.dumps(measurement)))

            return measurement

    def update_fields(self, measurement_id: str, fields: list[FieldValue],
                     actor: str, reason: str) -> dict:
        """
        Update measurement fields (creates new revision).

        Args:
            measurement_id: Measurement ID
            fields: Updated field values
            actor: User making the update
            reason: Reason for update

        Returns:
            Updated measurement document
        """
        with self.db.transaction('IMMEDIATE') as conn:
            row = conn.execute('''
                SELECT document, measurement_state
                FROM photo_measurements
                WHERE id=?
            ''', (measurement_id,)).fetchone()

            if not row:
                raise ValueError(f'Measurement {measurement_id} not found')

            measurement = json.loads(row['document'])
            current_state = row['measurement_state']

            if current_state == MeasurementState.CONFIRMED.value:
                raise ValueError('Cannot modify confirmed measurement')

            if current_state == MeasurementState.REJECTED.value:
                raise ValueError('Cannot modify rejected measurement')

            # Create revision record
            now = datetime.now(timezone.utc).isoformat()
            new_revision = measurement['revision'] + 1

            revision = {
                'revision': new_revision,
                'actor': actor,
                'timestamp': now,
                'reason': reason,
                'previous_fields': measurement.get('fields', []),
                'new_fields': [f.model_dump() for f in fields]
            }

            measurement['revision_history'].append(revision)
            measurement['fields'] = [f.model_dump() for f in fields]
            measurement['revision'] = new_revision
            measurement['updated_at'] = now
            measurement['state'] = MeasurementState.NEEDS_CORRECTION.value

            conn.execute('''
                UPDATE photo_measurements
                SET document=?, measurement_state=?, updated_at=?
                WHERE id=?
            ''', (json.dumps(measurement), MeasurementState.NEEDS_CORRECTION.value,
                  now, measurement_id))

            return measurement

    def submit_for_confirmation(self, measurement_id: str, actor: str) -> dict:
        """
        Submit measurement for final confirmation.

        Args:
            measurement_id: Measurement ID
            actor: User submitting

        Returns:
            Updated measurement document
        """
        with self.db.transaction('IMMEDIATE') as conn:
            row = conn.execute('''
                SELECT document, measurement_state
                FROM photo_measurements
                WHERE id=?
            ''', (measurement_id,)).fetchone()

            if not row:
                raise ValueError(f'Measurement {measurement_id} not found')

            measurement = json.loads(row['document'])
            current_state = row['measurement_state']

            valid_states = [MeasurementState.DRAFT.value,
                          MeasurementState.NEEDS_CORRECTION.value]

            if current_state not in valid_states:
                raise ValueError(f'Cannot submit from state {current_state}')

            # Validate fields are present
            if not measurement.get('fields'):
                raise ValueError('Cannot submit measurement without field values')

            now = datetime.now(timezone.utc).isoformat()
            measurement['state'] = MeasurementState.PENDING_CONFIRMATION.value
            measurement['submitted_for_confirmation_at'] = now
            measurement['submitted_by'] = actor
            measurement['updated_at'] = now

            conn.execute('''
                UPDATE photo_measurements
                SET document=?, measurement_state=?, updated_at=?
                WHERE id=?
            ''', (json.dumps(measurement),
                  MeasurementState.PENDING_CONFIRMATION.value,
                  now, measurement_id))

            return measurement

    def confirm_measurement(self, measurement_id: str, actor: str,
                          expected_revision: int,
                          reason: Optional[str] = None) -> tuple[dict, str]:
        """
        Confirm measurement and create production record.

        Args:
            measurement_id: Measurement ID
            actor: User confirming (must have authority)
            expected_revision: Expected revision number (optimistic locking)
            reason: Optional confirmation reason

        Returns:
            Tuple of (measurement_document, experiment_record_id)

        Raises:
            ValueError: If measurement cannot be confirmed
        """
        with self.db.transaction('IMMEDIATE') as conn:
            row = conn.execute('''
                SELECT document, measurement_state
                FROM photo_measurements
                WHERE id=?
            ''', (measurement_id,)).fetchone()

            if not row:
                raise ValueError(f'Measurement {measurement_id} not found')

            measurement = json.loads(row['document'])
            current_state = row['measurement_state']

            if current_state != MeasurementState.PENDING_CONFIRMATION.value:
                raise ValueError(f'Measurement must be pending confirmation, current: {current_state}')

            if measurement['revision'] != expected_revision:
                raise ValueError(
                    f'Revision mismatch: expected {expected_revision}, '
                    f'current {measurement["revision"]}'
                )

            # Validate all required fields
            if not measurement.get('fields'):
                raise ValueError('No field values to confirm')

            # Create confirmation record
            now = datetime.now(timezone.utc).isoformat()
            confirmation_id = str(uuid.uuid4())

            confirmation = {
                'id': confirmation_id,
                'measurement_id': measurement_id,
                'revision': measurement['revision'],
                'actor': actor,
                'action': 'confirm',
                'reason': reason,
                'confirmed_at': now,
                'fields_snapshot': measurement['fields']
            }

            conn.execute('''
                INSERT INTO measurement_confirmations
                VALUES(?,?,?,?,?,?,?,?)
            ''', (confirmation_id, measurement_id, measurement['revision'],
                  actor, 'confirm', reason, now, json.dumps(measurement['fields'])))

            # Create experiment record
            record_id = str(uuid.uuid4())
            experiment_record = {
                'id': record_id,
                'measurement_id': measurement_id,
                'confirmed_by': actor,
                'confirmed_at': now,
                'revision': measurement['revision'],
                'camera_id': measurement['camera_id'],
                'operator': measurement['operator'],
                'fields': measurement['fields'],
                'context': measurement['context'],
                'job_ids': measurement['job_ids'],
                'created_at': now
            }

            # Insert experiment record (production table)
            conn.execute('''
                INSERT INTO experiment_records(id, document)
                VALUES(?,?)
            ''', (record_id, json.dumps(experiment_record)))

            # Update measurement to confirmed state
            measurement['state'] = MeasurementState.CONFIRMED.value
            measurement['confirmed_at'] = now
            measurement['confirmed_by'] = actor
            measurement['experiment_record_id'] = record_id
            measurement['updated_at'] = now

            conn.execute('''
                UPDATE photo_measurements
                SET document=?, measurement_state=?, confirmed_at=?, confirmed_by=?
                WHERE id=?
            ''', (json.dumps(measurement), MeasurementState.CONFIRMED.value,
                  now, actor, measurement_id))

            return measurement, record_id

    def reject_measurement(self, measurement_id: str, actor: str,
                         expected_revision: int, reason: str) -> dict:
        """
        Reject a measurement.

        Args:
            measurement_id: Measurement ID
            actor: User rejecting
            expected_revision: Expected revision number
            reason: Rejection reason (required)

        Returns:
            Updated measurement document
        """
        with self.db.transaction('IMMEDIATE') as conn:
            row = conn.execute('''
                SELECT document, measurement_state
                FROM photo_measurements
                WHERE id=?
            ''', (measurement_id,)).fetchone()

            if not row:
                raise ValueError(f'Measurement {measurement_id} not found')

            measurement = json.loads(row['document'])
            current_state = row['measurement_state']

            if current_state not in [MeasurementState.PENDING_CONFIRMATION.value,
                                    MeasurementState.NEEDS_CORRECTION.value]:
                raise ValueError(f'Cannot reject from state {current_state}')

            if measurement['revision'] != expected_revision:
                raise ValueError('Revision mismatch')

            # Record rejection
            now = datetime.now(timezone.utc).isoformat()
            confirmation_id = str(uuid.uuid4())

            conn.execute('''
                INSERT INTO measurement_confirmations
                VALUES(?,?,?,?,?,?,?,?)
            ''', (confirmation_id, measurement_id, measurement['revision'],
                  actor, 'reject', reason, now, None))

            # Update measurement
            measurement['state'] = MeasurementState.REJECTED.value
            measurement['rejected_at'] = now
            measurement['rejected_by'] = actor
            measurement['rejection_reason'] = reason
            measurement['updated_at'] = now

            conn.execute('''
                UPDATE photo_measurements
                SET document=?, measurement_state=?
                WHERE id=?
            ''', (json.dumps(measurement), MeasurementState.REJECTED.value,
                  measurement_id))

            return measurement

    def get_measurement(self, measurement_id: str) -> Optional[dict]:
        """Get measurement by ID."""
        with self.db.connection() as conn:
            row = conn.execute('''
                SELECT document FROM photo_measurements WHERE id=?
            ''', (measurement_id,)).fetchone()

            if row:
                return json.loads(row['document'])
            return None

    def list_pending_confirmations(self, limit: int = 50) -> list[dict]:
        """List measurements pending confirmation."""
        with self.db.connection() as conn:
            rows = conn.execute('''
                SELECT document FROM photo_measurements
                WHERE measurement_state=?
                ORDER BY updated_at DESC
                LIMIT ?
            ''', (MeasurementState.PENDING_CONFIRMATION.value, limit)).fetchall()

            return [json.loads(row['document']) for row in rows]
