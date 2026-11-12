// Login / register screen, shown only when the backend requires an account.

import { LogIn, UserPlus } from "lucide-react";
import clsx from "clsx";
import { type FormEvent, useState } from "react";

import type { AuthConfig, SessionResponse } from "../api";
import { ApiError, login, register } from "../api";
import { AuthPrivacyNotice } from "./AuthPrivacyNotice";
import { BrandHeader } from "./Brand";

interface AuthScreenProps {
  config: AuthConfig;
  // Why the user is here again (session expired, account deleted…).
  notice: string | null;
  onSignedIn: (session: SessionResponse) => void;
}

const INPUT =
  "mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-[14.5px] text-ink outline-none transition focus:border-primary";

function failure(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof TypeError) return "No hay conexión con el servicio. Comprueba que está en marcha y tu conexión a internet.";
  return "Ha ocurrido un error inesperado.";
}

export function AuthScreen({ config, notice, onSignedIn }: AuthScreenProps) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [acceptsData, setAcceptsData] = useState(false);
  const [acceptsDocuments, setAcceptsDocuments] = useState(false);
  const [acceptsEmail, setAcceptsEmail] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [noticeOpen, setNoticeOpen] = useState(false);

  const registering = mode === "register";
  const minLength = config.password_min_length;

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (registering && password.length < minLength) {
      setError(`La contraseña debe tener al menos ${minLength} caracteres.`);
      return;
    }
    if (registering && !acceptsData) {
      setError("Para crear la cuenta tienes que aceptar el tratamiento de tus datos.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const session = registering
        ? await register({
            email,
            password,
            consents: {
              tratamiento_datos: acceptsData,
              envio_documentos_a_proveedor_llm: acceptsDocuments,
              avisos_por_correo: acceptsEmail,
            },
          })
        : await login({ email, password });
      onSignedIn(session);
    } catch (err) {
      setError(failure(err));
      setBusy(false);
    }
  }

  function switchMode(next: "login" | "register") {
    setMode(next);
    setError(null);
  }

  return (
    <div className="flex min-h-[100dvh] w-screen items-center justify-center bg-paper px-4 py-8">
      <main className="w-full max-w-[420px]">
        <div className="flex justify-center">
          <BrandHeader />
        </div>
        <div className="mt-5 rounded-xl border border-line bg-surface px-5 py-5 shadow-card">
          <div role="tablist" aria-label="Acceso" className="flex gap-1 rounded-lg bg-paper p-1">
            {(["login", "register"] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                role="tab"
                aria-selected={mode === tab}
                onClick={() => switchMode(tab)}
                className={clsx(
                  "flex-1 rounded-md px-3 py-1.5 text-[13.5px] font-semibold transition",
                  mode === tab ? "bg-surface text-ink shadow-card" : "text-muted hover:text-ink-soft",
                )}
              >
                {tab === "login" ? "Iniciar sesión" : "Crear cuenta"}
              </button>
            ))}
          </div>

          {notice && !error && (
            <p role="status" className="mt-4 rounded-lg border border-line bg-paper px-3 py-2 text-[13px] text-ink-soft">
              {notice}
            </p>
          )}

          <form onSubmit={(event) => void handleSubmit(event)} className="mt-4 space-y-3.5" noValidate>
            <label className="block text-[13px] font-semibold text-ink-soft">
              Correo electrónico
              <input
                type="email"
                name="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className={INPUT}
              />
            </label>
            <label className="block text-[13px] font-semibold text-ink-soft">
              Contraseña
              <input
                type="password"
                name="password"
                autoComplete={registering ? "new-password" : "current-password"}
                required
                minLength={registering ? minLength : undefined}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={INPUT}
              />
              {registering && <span className="mt-1 block text-[12px] font-normal text-muted">Al menos {minLength} caracteres.</span>}
            </label>

            {registering && (
              <fieldset className="space-y-2.5 border-t border-line pt-3.5">
                <legend className="sr-only">Consentimientos</legend>
                <label className="flex items-start gap-2.5 text-[13px] leading-snug text-ink-soft">
                  <input
                    type="checkbox"
                    name="tratamiento_datos"
                    checked={acceptsData}
                    onChange={(e) => {
                      setAcceptsData(e.target.checked);
                      setError(null);
                    }}
                    className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
                  />
                  <span>
                    Acepto que se guarden y traten los datos fiscales, familiares y de salud (discapacidad) que aporte, para recibir
                    asesoramiento.{" "}
                    <button type="button" onClick={() => setNoticeOpen(true)} className="font-semibold text-primary-dark underline">
                      Qué hacemos con tus datos
                    </button>{" "}
                    <span className="text-muted">(obligatorio)</span>
                  </span>
                </label>
                <label className="flex items-start gap-2.5 text-[13px] leading-snug text-ink-soft">
                  <input
                    type="checkbox"
                    name="envio_documentos_a_proveedor_llm"
                    checked={acceptsDocuments}
                    onChange={(e) => setAcceptsDocuments(e.target.checked)}
                    className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
                  />
                  <span>
                    Acepto que los documentos que suba (nóminas, certificados…) se envíen al proveedor del modelo de lenguaje para leerlos.{" "}
                    <span className="text-muted">(opcional; sin él no podrás subir documentos)</span>
                  </span>
                </label>
                <label className="flex items-start gap-2.5 text-[13px] leading-snug text-ink-soft">
                  <input
                    type="checkbox"
                    name="avisos_por_correo"
                    checked={acceptsEmail}
                    onChange={(e) => setAcceptsEmail(e.target.checked)}
                    className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
                  />
                  <span>
                    Quiero recibir por correo los avisos de plazos y cambios que me afecten, cuando el servicio los ofrezca.{" "}
                    <span className="text-muted">(opcional)</span>
                  </span>
                </label>
              </fieldset>
            )}

            {error && (
              <p role="alert" className="rounded-lg border border-risk-high-fg/30 bg-risk-high-bg px-3 py-2 text-[13px] text-ink-soft">
                {error}
              </p>
            )}

            <button
              type="submit"
              disabled={busy || !email || !password}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-[14.5px] font-semibold text-white transition hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-50"
            >
              {registering ? <UserPlus size={16} aria-hidden /> : <LogIn size={16} aria-hidden />}
              {busy ? "Un momento…" : registering ? "Crear cuenta" : "Entrar"}
            </button>
          </form>
        </div>
        <p className="mt-4 text-center text-[12.5px] leading-relaxed text-muted">
          Tu expediente solo es visible con tu cuenta.{" "}
          <button type="button" onClick={() => setNoticeOpen(true)} className="font-semibold text-primary-dark underline">
            Privacidad
          </button>
        </p>
      </main>
      <AuthPrivacyNotice open={noticeOpen} onClose={() => setNoticeOpen(false)} noticeUrl={config.privacy_notice_url} />
    </div>
  );
}
