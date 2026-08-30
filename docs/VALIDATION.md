# Validation Record

Validation completed before packaging:

- Python compilation passed for `app/` and `alembic/`.
- SQLAlchemy model mapper configuration passed.
- OpenAPI generation passed with 50 documented API paths.
- Every request in the Postman collection matched an OpenAPI route and HTTP method.
- Four core automated tests passed.
- Full local workflow passed against an isolated SQLite database:
  - admin login
  - PED OTP request and verification
  - registration creation
  - student creation
  - student photo and bonafide upload
  - test Razorpay order and signature verification
  - admin approval and QR creation
  - QR roster scan
  - student-wise attendance confirmation
  - certificate template upload
  - attendance-based certificate generation and publication
  - PED certificate download
  - CSV export
  - Excel workbook export
- Generated certificate PDF was rendered and visually checked for clipping and broken text.

Production credentials and live Brevo/Razorpay/Supabase calls are intentionally not included or executed. Configure them through environment variables and run the same Postman flow with test credentials before production launch.
