-- PDF-only recovery never reserves entries, changes status, or reissues an invoice.
create or replace function public.store_manual_invoice_pdf(
  p_invoice_id uuid, p_invoice_number text, p_net_total numeric,
  p_gross_total numeric, p_pdf_base64 text
) returns void language plpgsql security definer set search_path = '' as $$
declare r public.invoice_ledger; v_pdf bytea;
begin
  select * into r from public.invoice_ledger where id = p_invoice_id for update;
  if r.id is null or not private.can_manage_company_invoices(r.company_id) then
    raise sqlstate '42501' using message = 'Invoice was not found or is not accessible';
  end if;
  if r.status <> 'issued' or r.invoice_number is distinct from p_invoice_number
    or r.net_total is distinct from p_net_total or r.gross_total is distinct from p_gross_total then
    raise sqlstate 'PT409' using message = 'Provider invoice details must match the issued ledger';
  end if;
  if p_pdf_base64 is null or length(p_pdf_base64) > 34952536 then
    raise sqlstate '22023' using message = 'Invoice PDF is missing or too large';
  end if;
  v_pdf := decode(p_pdf_base64, 'base64');
  if octet_length(v_pdf) = 0 or octet_length(v_pdf) > 26214400
    or substring(v_pdf from 1 for 4) <> decode('25504446', 'hex') then
    raise sqlstate '22023' using message = 'Invoice PDF is invalid or too large';
  end if;
  update public.invoice_ledger set pdf_bytes = v_pdf, pdf_available = true
    where id = r.id and pdf_bytes is null;
end $$;
revoke all on function public.store_manual_invoice_pdf(uuid,text,numeric,numeric,text) from public, anon;
grant execute on function public.store_manual_invoice_pdf(uuid,text,numeric,numeric,text) to authenticated;
