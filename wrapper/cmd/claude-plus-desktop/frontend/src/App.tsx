import Terminal from "./Terminal";
import Sessions from "./Sessions";

function App() {
  return (
    <div style={{ display: "flex", height: "100vh", margin: 0 }}>
      <Sessions />
      <main style={{ flex: 1, minWidth: 0, background: "#1e1e1e" }}>
        <Terminal />
      </main>
    </div>
  );
}

export default App;
