// The user's privacy controls: consents, a copy of everything stored about
// them, and erasing the account. Only exists when the backend has accounts.

import { Download, ShieldCheck, Trash2, X } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";

import type { ConsentState, ConsentType } from "../api";
import { ApiError, deleteMyAccount, exportMyData, getConsents, updateConsents } from "../api";
import { formatDate } from "../lib/presentation";
import { loadConversations } from "../lib/storage";
import { AuthPrivacyNotice } from "./AuthPrivacyNotice";

interface PrivacyPanelProps {
  open: boolean;
  onClose: () => void;
  email: string;
  noticeUrl: string;
  // The account no longer exists on the server: drop the session and local data.
  onAccountDeleted: () => void;
}

const CONSENT_TEXT: Record<ConsentType, { title: string; detail: string }> = {
  tratamiento_datos: {
    title: "Tratamiento de mis datos",
    detail:
      "Guardar y tratar los datos fiscales, familiares y de salud (discapacidad) que aportas, para asesorarte. Es imprescindible para tener cuenta: para retirarlo, elimina tu cuenta.",
  },
  envio_documentos_a_proveedor_llm: {
    title: "Enviar mis documentos al proveedor del modelo de lenguaje",
    detail: "Necesario para que el asesor lea los documentos que subes. Sin él, la subida de documentos queda desactivada. Los documentos no se almacenan.",
  },
  avisos_por_correo: {
    title: "Avisos por correo",
    detail: "Recibir por correo los avisos de plazos y cambios que te afecten, cuando el servicio los ofrezca. Los avisos dentro de la aplicación no dependen de esto.",
  },
};

const ORDER: ConsentType[] = ["tratamiento_datos", "envio_documentos_a_proveedor_llm", "avisos_por_correo"];

function failure(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof TypeError) return "No hay conexión con el servicio.";
  return fallback;
}

function download(filename: string, data: unknown): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function PrivacyPanel({ open, onClose, email, noticeUrl, onAccountDeleted }: PrivacyPanelProps) {
  const [consents, setConsents] = useState<ConsentState[] | null>(null);
  const [consentsError, setConsentsError] = useState<string | null>(null);
  const [savingConsent, setSavingConsent] = useState<ConsentType | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportMessage, setExportMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [password, setPassword] = useState("");
  const [understood, setUnderstood] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [noticeOpen, setNoticeOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    getConsents(controller.signal).then(
      (resp) => {
        setConsents(resp.consents);
        setConsentsError(null);
      },
      (err: unknown) => {
        if (!controller.signal.aborted) setConsentsError(failure(err, "No se pudieron cargar tus consentimientos."));
      },
    );
    return () => controller.abort();
  }, [open]);

  if (!open) return null;

  function handleToggle(type: ConsentType, granted: boolean) {
    if (savingConsent) return;
    setSavingConsent(type);
    setConsentsError(null);
    updateConsents({ [type]: granted })
      .then(
        (resp) => setConsents(resp.consents),
        (err: unknown) => setConsentsError(failure(err, "No se pudo guardar el cambio.")),
      )
      .finally(() => setSavingConsent(null));
  }

  function handleExport() {
    if (exporting) return;
    setExporting(true);
    setExportMessage(null);
    exportMyData()
      .then(
        (data) => {
          // The conversations live only in this browser; add them so the copy is complete.
          download(`haciendagpt-mis-datos-${new Date().toISOString().slice(0, 10)}.json`, {
            ...data,
            conversaciones_en_este_navegador: loadConversations(),
          });
          setExportMessage({ ok: true, text: "Descarga preparada. El archivo contiene datos personales: guárdalo en un lugar seguro." });
        },
        (err: unknown) => setExportMessage({ ok: false, text: failure(err, "No se pudieron exportar tus datos.") }),
      )
      .finally(() => setExporting(false));
  }

  function handleDelete(event: FormEvent) {
    event.preventDefault();
    if (deleting || !password || !understood) return;
    if (!window.confirm("Se borrarán tu cuenta, tu expediente y tus avisos. No se puede deshacer. ¿Eliminar definitivamente?")) return;
    setDeleting(true);
    setDeleteError(null);
    deleteMyAccount(password).then(
      () => onAccountDeleted(),
      (err: unknown) => {
        setDeleteError(failure(err, "No se pudo eliminar la cuenta."));
        setDeleting(false);
      },
    );
  }

  const byType = new Map((consents ?? []).map((c) => [c.type, c]));

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <button type="button" aria-label="Cerrar privacidad y datos" className="absolute inset-0 bg-ink/40" onClick={onClose} />
      <aside role="dialog" aria-modal="true" aria-label="Privacidad y datos" className="relative flex h-full w-full max-w-[440px] flex-col border-l border-line bg-paper shadow-pop">
        <header className="flex items-center justify-between border-b border-line px-5 py-4">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 font-serif text-[19px] font-bold text-ink">
              <ShieldCheck size={18} aria-hidden className="text-primary-dark" />
              Privacidad y datos
            </h2>
            <p className="truncate text-[12.5px] text-muted">{email}</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Cerrar" className="rounded-lg p-2 text-muted hover:bg-line/60 hover:text-ink">
            <X size={18} />
          </button>
        </header>

        <div className="flex-1 space-y-6 overflow-y-auto px-5 py-5">
          <section aria-labelledby="privacy-consents">
            <h3 id="privacy-consents" className="text-[11.5px] font-semibold uppercase tracking-[0.08em] text-muted">
              Consentimientos
            </h3>
            {consentsError && (
              <p role="alert" className="mt-2 rounded-lg border border-risk-high-fg/30 bg-risk-high-bg px-3 py-2 text-[13px] text-ink-soft">
                {consentsError}
              </p>
            )}
            {!consents && !consentsError && <p className="mt-2 text-[13.5px] text-muted">Cargando…</p>}
            {consents && (
              <ul className="mt-2 space-y-2.5">
                {ORDER.map((type) => {
                  const state = byType.get(type);
                  const granted = state?.granted ?? false;
                  const locked = type === "tratamiento_datos";
                  return (
                    <li key={type} className="rounded-xl border border-line bg-surface px-4 py-3 shadow-card">
                      <label className="flex items-start gap-3">
                        <input
                          type="checkbox"
                          name={type}
                          checked={granted}
                          disabled={locked || savingConsent !== null}
                          onChange={(e) => handleToggle(type, e.target.checked)}
                          className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
                        />
                        <span className="min-w-0">
                          <span className="block text-[14px] font-bold leading-snug text-ink">{CONSENT_TEXT[type].title}</span>
                          <span className="mt-0.5 block text-[12.5px] leading-snug text-ink-soft">{CONSENT_TEXT[type].detail}</span>
                          <span className="mt-1 block text-[11.5px] text-muted">
                            {state?.timestamp
                              ? `${granted ? "Concedido" : "No concedido"} el ${formatDate(state.timestamp.slice(0, 10))}`
                              : "Todavía no has decidido"}
                          </span>
                        </span>
                      </label>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          <section aria-labelledby="privacy-export">
            <h3 id="privacy-export" className="text-[11.5px] font-semibold uppercase tracking-[0.08em] text-muted">
              Una copia de mis datos
            </h3>
            <p className="mt-2 text-[13.5px] leading-relaxed text-ink-soft">
              Descarga en un archivo JSON todo lo que hay guardado sobre ti: tu perfil, tus expedientes con su historial, tus avisos y tus
              consentimientos, más las conversaciones guardadas en este navegador.
            </p>
            <button
              type="button"
              onClick={handleExport}
              disabled={exporting}
              className="mt-2.5 inline-flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-[13.5px] font-semibold text-ink-soft transition hover:border-primary hover:text-primary-dark disabled:opacity-50"
            >
              <Download size={15} aria-hidden />
              {exporting ? "Preparando…" : "Exportar mis datos"}
            </button>
            {exportMessage && (
              <p
                role={exportMessage.ok ? "status" : "alert"}
                className={
                  exportMessage.ok
                    ? "mt-2 text-[12.5px] text-muted"
                    : "mt-2 rounded-lg border border-risk-high-fg/30 bg-risk-high-bg px-3 py-2 text-[13px] text-ink-soft"
                }
              >
                {exportMessage.text}
              </p>
            )}
          </section>

          <section aria-labelledby="privacy-delete">
            <h3 id="privacy-delete" className="text-[11.5px] font-semibold uppercase tracking-[0.08em] text-muted">
              Eliminar mi cuenta
            </h3>
            <p className="mt-2 text-[13.5px] leading-relaxed text-ink-soft">
              Se borran del servidor tu cuenta, tus expedientes con su historial, tus avisos, tus consentimientos y tus sesiones, y de este
              navegador tus conversaciones. <strong className="text-ink">No se puede deshacer.</strong>
            </p>
            <form onSubmit={handleDelete} className="mt-2.5 space-y-2.5">
              <label className="block text-[13px] font-semibold text-ink-soft">
                Confirma tu contraseña
                <input
                  type="password"
                  name="delete-password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-[14.5px] text-ink outline-none transition focus:border-primary"
                />
              </label>
              <label className="flex items-start gap-2.5 text-[13px] leading-snug text-ink-soft">
                <input
                  type="checkbox"
                  name="delete-understood"
                  checked={understood}
                  onChange={(e) => setUnderstood(e.target.checked)}
                  className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
                />
                Entiendo que mis datos se borrarán y no podré recuperarlos.
              </label>
              {deleteError && (
                <p role="alert" className="rounded-lg border border-risk-high-fg/30 bg-risk-high-bg px-3 py-2 text-[13px] text-ink-soft">
                  {deleteError}
                </p>
              )}
              <button
                type="submit"
                disabled={deleting || !password || !understood}
                className="inline-flex items-center gap-2 rounded-lg border border-risk-high-fg/40 bg-risk-high-bg px-3 py-2 text-[13.5px] font-semibold text-risk-high-fg transition hover:border-risk-high-fg disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Trash2 size={15} aria-hidden />
                {deleting ? "Eliminando…" : "Eliminar mi cuenta y mis datos"}
              </button>
            </form>
          </section>
        </div>
        <footer className="border-t border-line px-5 py-3 text-[11.5px] leading-relaxed text-muted">
          <button type="button" onClick={() => setNoticeOpen(true)} className="font-semibold text-primary-dark underline">
            Qué hacemos con tus datos
          </button>
          {noticeUrl && (
            <>
              {" · "}
              <a href={noticeUrl} target="_blank" rel="noreferrer" className="font-semibold text-primary-dark underline">
                Aviso de privacidad
              </a>
            </>
          )}
        </footer>
      </aside>
      <AuthPrivacyNotice open={noticeOpen} onClose={() => setNoticeOpen(false)} noticeUrl={noticeUrl} />
    </div>
  );
}
