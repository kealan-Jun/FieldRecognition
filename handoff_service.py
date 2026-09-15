"""Instrument handoff service for multi-user workflows."""
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from database import Database


class HandoffState(str, Enum):
    """Handoff request states."""
    PENDING = 'pending'
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'


class HandoffRequest(BaseModel):
    """Instrument handoff request."""
    id: str
    binding_id: str
    instrument_id: str
    offered_by: str
    offered_to: str
    offered_at: str
    state: HandoffState
    expires_at: Optional[str] = None
    message: Optional[str] = None


class HandoffService:
    """Manage instrument handoffs between users."""

    def __init__(self, db: Database):
        self.db = db
        self.default_expiry_hours = 24

    def request_handoff(self, binding_id: str, instrument_id: str,
                       offered_by: str, offered_to: str,
                       message: Optional[str] = None) -> HandoffRequest:
        """
        Request to hand off an instrument to another user.

        Args:
            binding_id: Current binding ID
            instrument_id: Instrument being handed off
            offered_by: User offering (must own the binding)
            offered_to: User receiving the offer
            message: Optional message

        Returns:
            Created handoff request

        Raises:
            ValueError: If user doesn't own binding or instrument not bound
        """
        with self.db.transaction('IMMEDIATE') as conn:
            # Verify binding ownership
            binding_row = conn.execute('''
                SELECT document FROM bindings WHERE id=? AND ended IS NULL
            ''', (binding_id,)).fetchone()

            if not binding_row:
                raise ValueError('Binding not found or already ended')

            binding = json.loads(binding_row['document'])

            # Check ownership
            if binding.get('operator') != offered_by:
                raise ValueError('Only the current operator can offer handoff')

            # Check instrument matches
            if binding.get('instrument_id') != instrument_id:
                raise ValueError('Instrument does not match binding')

            # Check for existing pending handoffs
            existing = conn.execute('''
                SELECT id FROM binding_handoffs
                WHERE binding_id=? AND state='pending'
            ''', (binding_id,)).fetchone()

            if existing:
                raise ValueError('Handoff already pending for this binding')

            # Create handoff request
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            expires = now + timedelta(hours=self.default_expiry_hours)

            handoff_id = str(uuid.uuid4())
            document = {
                'id': handoff_id,
                'binding_id': binding_id,
                'instrument_id': instrument_id,
                'offered_by': offered_by,
                'offered_to': offered_to,
                'offered_at': now.isoformat(),
                'expires_at': expires.isoformat(),
                'message': message,
                'state': HandoffState.PENDING.value
            }

            conn.execute('''
                INSERT INTO binding_handoffs
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (handoff_id, binding_id, instrument_id,
                  offered_by, offered_to, now.isoformat(),
                  HandoffState.PENDING.value, None, None, None,
                  'pending', json.dumps(document)))

            return HandoffRequest(**document)

    def accept_handoff(self, handoff_id: str, accepted_by: str) -> dict:
        """
        Accept a handoff request and transfer binding.

        Args:
            handoff_id: Handoff request ID
            accepted_by: User accepting (must match offered_to)

        Returns:
            New binding document

        Raises:
            ValueError: If handoff invalid or user mismatch
        """
        with self.db.transaction('IMMEDIATE') as conn:
            # Get handoff
            row = conn.execute('''
                SELECT document FROM binding_handoffs WHERE id=?
            ''', (handoff_id,)).fetchone()

            if not row:
                raise ValueError('Handoff request not found')

            handoff = json.loads(row['document'])

            # Verify state
            if handoff['state'] != HandoffState.PENDING.value:
                raise ValueError(f'Handoff is {handoff["state"]}, not pending')

            # Verify acceptor
            if handoff['offered_to'] != accepted_by:
                raise ValueError('Only the offered-to user can accept')

            # Check expiry
            now = datetime.now(timezone.utc).isoformat()
            if handoff.get('expires_at') and now > handoff['expires_at']:
                # Mark as expired
                conn.execute('''
                    UPDATE binding_handoffs SET state='expired' WHERE id=?
                ''', (handoff_id,))
                raise ValueError('Handoff request has expired')

            # End old binding
            old_binding_row = conn.execute('''
                SELECT document FROM bindings WHERE id=?
            ''', (handoff['binding_id'],)).fetchone()

            if not old_binding_row:
                raise ValueError('Original binding not found')

            old_binding = json.loads(old_binding_row['document'])

            conn.execute('''
                UPDATE bindings SET ended=? WHERE id=?
            ''', (now, handoff['binding_id']))

            # Create new binding
            new_binding_id = str(uuid.uuid4())
            new_binding = old_binding.copy()
            new_binding.update({
                'id': new_binding_id,
                'operator': accepted_by,
                'started_at': now,
                'ended': None,
                'handoff_from': handoff['binding_id'],
                'handoff_request': handoff_id
            })

            conn.execute('''
                INSERT INTO bindings VALUES(?,?,?,?)
            ''', (new_binding_id, new_binding['camera'],
                  None, json.dumps(new_binding)))

            # Mark handoff accepted
            conn.execute('''
                UPDATE binding_handoffs SET state='accepted', accepted_at=? WHERE id=?
            ''', (now, handoff_id))

            return new_binding

    def reject_handoff(self, handoff_id: str, rejected_by: str, reason: str):
        """
        Reject a handoff request.

        Args:
            handoff_id: Handoff request ID
            rejected_by: User rejecting
            reason: Rejection reason
        """
        with self.db.transaction() as conn:
            row = conn.execute('''
                SELECT document FROM binding_handoffs WHERE id=?
            ''', (handoff_id,)).fetchone()

            if not row:
                raise ValueError('Handoff request not found')

            handoff = json.loads(row['document'])

            if handoff['state'] != HandoffState.PENDING.value:
                raise ValueError(f'Handoff is {handoff["state"]}, cannot reject')

            # Either party can reject
            if rejected_by not in [handoff['offered_by'], handoff['offered_to']]:
                raise ValueError('Only involved parties can reject')

            now = datetime.now(timezone.utc).isoformat()

            conn.execute('''
                UPDATE binding_handoffs
                SET state='rejected', rejected_at=?, rejection_reason=?
                WHERE id=?
            ''', (now, reason, handoff_id))

    def cancel_handoff(self, handoff_id: str, cancelled_by: str):
        """Cancel a pending handoff (by offerer only)."""
        with self.db.transaction() as conn:
            row = conn.execute('''
                SELECT document FROM binding_handoffs WHERE id=?
            ''', (handoff_id,)).fetchone()

            if not row:
                raise ValueError('Handoff request not found')

            handoff = json.loads(row['document'])

            if handoff['state'] != HandoffState.PENDING.value:
                raise ValueError('Only pending handoffs can be cancelled')

            if handoff['offered_by'] != cancelled_by:
                raise ValueError('Only the offerer can cancel')

            conn.execute('''
                UPDATE binding_handoffs SET state='cancelled' WHERE id=?
            ''', (handoff_id,))

    def list_pending_handoffs(self, user_id: str) -> list[HandoffRequest]:
        """List pending handoffs for a user (offered to them)."""
        with self.db.connection() as conn:
            rows = conn.execute('''
                SELECT document FROM binding_handoffs
                WHERE offered_to=? AND state='pending'
                ORDER BY offered_at DESC
            ''', (user_id,)).fetchall()

            return [HandoffRequest(**json.loads(row['document'])) for row in rows]
