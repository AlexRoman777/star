import { Link } from "react-router-dom";

export interface HubTabItem {
  key: string;
  to: string;
  label: string;
}

/** The one tab switcher every hub page (Photos, Work, Machines, Settings) uses. */
export function HubTabs({
  ariaLabel,
  active,
  items,
  secondary = false,
}: {
  ariaLabel: string;
  active: string;
  items: ReadonlyArray<HubTabItem>;
  secondary?: boolean;
}) {
  return (
    <nav
      aria-label={ariaLabel}
      className={secondary ? "page-view-switcher secondary-switcher" : "page-view-switcher hub-switcher"}
    >
      {items.map((item) => (
        <Link
          aria-current={active === item.key ? "page" : undefined}
          className={active === item.key ? "active" : ""}
          key={item.key}
          to={item.to}
        >
          {item.label}
        </Link>
      ))}
    </nav>
  );
}
