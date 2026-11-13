import ReactDOM from "react-dom/client";
import { RouterProvider } from "@tanstack/react-router";
import { getRouter } from "./router";
import { ArcProvider } from "./components/arc/lib/arc-provider";

const router = getRouter();

const rootElement = document.getElementById("app")!;

if (!rootElement.innerHTML) {
  const root = ReactDOM.createRoot(rootElement);
  root.render(
    <ArcProvider>
      <RouterProvider router={router} />
    </ArcProvider>,
  );
}
