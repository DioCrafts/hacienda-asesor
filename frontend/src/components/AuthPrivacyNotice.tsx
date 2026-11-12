// Plain-language summary of what the service does with the user's data, shown
// from the register screen and the privacy panel. It describes what the
// SOFTWARE does; the operator's legal privacy notice (who the controller is,
// legal basis, retention…) is a separate text the operator must publish.

import { ShieldCheck, X } from "lucide-react";
import { useEffect } from "react";

interface AuthPrivacyNoticeProps {
  open: boolean;
  onClose: () => void;
  // The operator's own privacy notice, when it has published one.
  noticeUrl: string;
}

export function AuthPrivacyNotice({ open, onClose, noticeUrl }: AuthPrivacyNoticeProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-3">
      <button type="button" aria-label="Cerrar la información de privacidad" className="absolute inset-0 bg-ink/40" onClick={onClose} />
      <section
        role="dialog"
        aria-modal="true"
        aria-label="Qué hacemos con tus datos"
        className="relative flex max-h-full w-full max-w-[560px] flex-col rounded-xl border border-line bg-paper shadow-pop"
      >
        <header className="flex items-center justify-between border-b border-line px-5 py-4">
          <h2 className="flex items-center gap-2 font-serif text-[19px] font-bold text-ink">
            <ShieldCheck size={18} aria-hidden className="text-primary-dark" />
            Qué hacemos con tus datos
          </h2>
          <button type="button" onClick={onClose} aria-label="Cerrar" className="rounded-lg p-2 text-muted hover:bg-line/60 hover:text-ink">
            <X size={18} />
          </button>
        </header>
        <div className="space-y-4 overflow-y-auto px-5 py-5 text-[13.5px] leading-relaxed text-ink-soft">
          <div>
            <h3 className="text-[14px] font-bold text-ink">Qué se guarda</h3>
            <ul className="mt-1 list-disc space-y-1 pl-5">
              <li>Tu correo y una huella irreversible de tu contraseña (nunca la contraseña).</li>
              <li>
                Tu expediente: los datos fiscales, familiares y, si los aportas, de discapacidad que le cuentas al asesor, junto con el
                registro de las consultas que los generaron.
              </li>
              <li>Los avisos que el asesor prepara para ti y los consentimientos que das o retiras, con su fecha.</li>
              <li>El texto de las conversaciones se guarda solo en este navegador, no en el servidor.</li>
            </ul>
          </div>
          <div>
            <h3 className="text-[14px] font-bold text-ink">Qué se envía al proveedor del modelo de lenguaje</h3>
            <ul className="mt-1 list-disc space-y-1 pl-5">
              <li>Tus preguntas, la conversación en curso y los datos de tu expediente que el asesor necesita para responder.</li>
              <li>
                El contenido de los documentos que subas, <strong className="text-ink">solo si das tu consentimiento expreso</strong>. Los
                documentos no se almacenan.
              </li>
              <li>El simulador, el calendario y los avisos se calculan sin enviar nada a ningún proveedor.</li>
            </ul>
          </div>
          <div>
            <h3 className="text-[14px] font-bold text-ink">Qué puedes hacer</h3>
            <ul className="mt-1 list-disc space-y-1 pl-5">
              <li>Descargar todo lo que hay guardado sobre ti, en cualquier momento.</li>
              <li>Eliminar tu cuenta: se borran tu expediente, tus avisos, tus consentimientos y tus sesiones. No se puede deshacer.</li>
              <li>Dar o retirar cada consentimiento cuando quieras, desde «Privacidad y datos».</li>
            </ul>
          </div>
          <p className="rounded-lg border border-line bg-surface px-3 py-2 text-[12.5px] text-muted">
            Este resumen describe lo que hace la aplicación. Quién es el responsable del tratamiento, con qué base legal trata tus datos,
            durante cuánto tiempo y cómo ejercer tus derechos lo indica el aviso de privacidad de quien te ofrece este servicio.{" "}
            {noticeUrl ? (
              <a href={noticeUrl} target="_blank" rel="noreferrer" className="font-semibold text-primary-dark underline">
                Leer el aviso de privacidad
              </a>
            ) : (
              <strong className="text-ink-soft">Este servicio todavía no ha publicado el suyo: pídeselo antes de aportar datos reales.</strong>
            )}
          </p>
        </div>
      </section>
    </div>
  );
}
