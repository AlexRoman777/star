import { Component, type ErrorInfo, type ReactNode } from "react";
import { useLocation } from "react-router-dom";

interface BoundaryState {
  error: Error | null;
}

class RouteBoundaryState extends Component<{ children: ReactNode }, BoundaryState> {
  override state: BoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("star.route_error", {
      message: error.message,
      name: error.name,
      stack: error.stack,
      componentStack: info.componentStack,
      location: window.location.href,
    });
  }

  override render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <section className="panel empty-state large" role="alert">
        <strong>This page could not be displayed</strong>
        <span>{this.state.error.message}</span>
        <button className="secondary" type="button" onClick={() => window.location.reload()}>
          Reload page
        </button>
      </section>
    );
  }
}

export function RouteErrorBoundary({ children }: { children: ReactNode }) {
  const location = useLocation();
  return <RouteBoundaryState key={`${location.pathname}${location.search}`}>{children}</RouteBoundaryState>;
}
