import type { FormEvent } from "react";

export type AuthenticationMode = "password" | "setup";

interface AuthenticationScreenProps {
  mode: AuthenticationMode;
  busy: boolean;
  error: string;
  setupAvailable: boolean;
  existingAdministrator: boolean;
  localPasswordSetupAvailable: boolean;
  selectMode: (mode: AuthenticationMode) => void;
  loginWithPassword: (event: FormEvent<HTMLFormElement>) => void;
  setupPassword: (event: FormEvent<HTMLFormElement>) => void;
}

type LoginPanelProps = Pick<
  AuthenticationScreenProps,
  | "busy"
  | "setupAvailable"
  | "existingAdministrator"
  | "selectMode"
  | "loginWithPassword"
>;

function LoginPanel(props: LoginPanelProps) {
  return (
    <>
      <form onSubmit={props.loginWithPassword}>
        <h2>Sign in</h2>
        <label>Username<input name="username" autoComplete="username" required autoFocus /></label>
        <label>Password<input name="password" type="password" autoComplete="current-password" required /></label>
        <button className="primary" disabled={props.busy}>{props.busy ? "Signing in…" : "Sign in"}</button>
      </form>
      {props.setupAvailable && (
        <div className="auth-setup-link">
          <small>{props.existingAdministrator ? "The existing administrator does not have a password yet." : "No administrator exists yet."}</small>
          <button type="button" className="secondary" onClick={() => props.selectMode("setup")}>{props.existingAdministrator ? "Set initial password" : "First setup"}</button>
        </div>
      )}
    </>
  );
}

type SetupPanelProps = Pick<
  AuthenticationScreenProps,
  | "busy"
  | "existingAdministrator"
  | "localPasswordSetupAvailable"
  | "selectMode"
  | "setupPassword"
>;

function SetupPanel(props: SetupPanelProps) {
  const setupTitle = props.existingAdministrator ? "Set initial password" : "Create administrator";
  const explanation = props.localPasswordSetupAvailable
    ? "This one-time setup is available only through the trusted local Star address."
    : "The bootstrap token proves local machine access. It is used only for this setup.";
  return (
    <>
      <button type="button" className="auth-back" onClick={() => props.selectMode("password")}>← Back to sign in</button>
      <form onSubmit={props.setupPassword}>
        <h2>{setupTitle}</h2>
        <p className="input-help">{explanation}</p>
        <label>Username<input name="username" autoComplete="username" required minLength={3} /></label>
        {!props.existingAdministrator && <label>Display name<input name="display_name" autoComplete="name" required /></label>}
        <label>New password<input name="password" type="password" autoComplete="new-password" minLength={8} required /></label>
        <label>Confirm password<input name="password_confirmation" type="password" autoComplete="new-password" minLength={8} required /></label>
        {!props.localPasswordSetupAvailable && <label>Bootstrap token<input name="bootstrap_token" type="password" autoComplete="off" required /></label>}
        <p className="input-help">Minimum 8 characters. A longer passphrase is recommended.</p>
        <button className="primary" disabled={props.busy}>{props.busy ? "Saving password…" : "Save password and sign in"}</button>
      </form>
    </>
  );
}

export function AuthenticationScreen(props: AuthenticationScreenProps) {
  return (
    <main className="auth-layout">
      <section className="auth-card">
        <header className="auth-brand">
          <span className="brand-mark"><img src="/favicon.svg" alt="" width="32" height="32" /></span>
          <span><h1>Star</h1><small>Private library control</small></span>
        </header>
        {props.mode === "setup" ? (
          <SetupPanel
            busy={props.busy}
            existingAdministrator={props.existingAdministrator}
            localPasswordSetupAvailable={props.localPasswordSetupAvailable}
            selectMode={props.selectMode}
            setupPassword={props.setupPassword}
          />
        ) : (
          <LoginPanel
            busy={props.busy}
            setupAvailable={props.setupAvailable}
            existingAdministrator={props.existingAdministrator}
            selectMode={props.selectMode}
            loginWithPassword={props.loginWithPassword}
          />
        )}
        {props.error && <p className="form-error" role="alert">{props.error}</p>}
      </section>
    </main>
  );
}
