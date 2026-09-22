-- The entities this conversation touches.
--
-- Every value here is either read back verbatim by a recorded tool result or is
-- a field the recorded results never revealed. The former are exact; the latter
-- are plausible and consistent with the call, and are marked as such.
--
-- Scenario clock: 2026-08-27T18:12:00-05:00. Miles Carter lost his albuterol
-- inhaler, leaves town the next morning, and the payer has already rejected the
-- refill as too soon.

BEGIN;

INSERT INTO patients
    (patient_id, full_name, date_of_birth, preferred_store_id,
     insurance_plan_id, allergies)
VALUES
    ('0e28b5d4-a93b-437e-9ba0-cbe4e7d9dbbb', 'Miles Carter', '1988-06-14', '8e22d41d-843c-4e73-95c0-3c9877366ba9',
     'plan-midwest-choice-ppo', '{}');

-- Masked form is read aloud, so it is stored exactly as the recorded results
-- render it rather than derived from a number the results never disclosed.
INSERT INTO notification_destinations
    (destination_id, patient_id, channel, masked_destination, verified)
VALUES
    ('94c55afb-c756-4c14-9f4a-0c0cf2e3d69e', '0e28b5d4-a93b-437e-9ba0-cbe4e7d9dbbb', 'sms',
     '***-***-on-file', TRUE);

-- prescriber, quantity, days_supply, refills_remaining and last_fill_date are
-- never returned by any recorded result; they are consistent filler so the row
-- is a complete record rather than a stub.
INSERT INTO prescriptions
    (prescription_id, patient_id, medication_id, prescriber, quantity,
     days_supply, refills_remaining, last_fill_date, received_at,
     prescription_valid, fill_store_id, workflow_status, customer_facing_status,
     priority_reason, payment_options, ready_alert_destination_id,
     notification_channel, ready_alert)
VALUES
    ('9670dbb4-6227-48ed-99fc-7ce746085502', '0e28b5d4-a93b-437e-9ba0-cbe4e7d9dbbb', '9dcd4906-4db3-4290-bccd-afde111823cb',
     'Dr. Elena Vasquez', '1 inhaler (8.5 g)', 30, 3, '2026-08-05',
     '2026-08-27T10:42:00-05:00', TRUE, '8e22d41d-843c-4e73-95c0-3c9877366ba9',
     'claim_rejected', 'processing', NULL,
     '{pay_at_pickup}', '94c55afb-c756-4c14-9f4a-0c0cf2e3d69e', NULL, NULL);

-- The rejection the call opens on. Rerunning the claim appends a new row rather
-- than editing this one, so the payer history stays intact.
INSERT INTO claims
    (prescription_id, status, reason, copay, currency, override_id, submitted_at)
VALUES
    ('9670dbb4-6227-48ed-99fc-7ce746085502', 'rejected', 'refill_too_soon', NULL, NULL, NULL,
     '2026-08-27T10:48:00-05:00');

INSERT INTO fill_queue
    (prescription_id, status, position, estimated_minutes,
     pharmacist_verification_required, priority_note)
VALUES
    ('9670dbb4-6227-48ed-99fc-7ce746085502', 'blocked_by_claim', NULL, NULL, NULL, 'absent');

COMMIT;
