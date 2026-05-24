import { createContext, useContext, useState } from "react";

const FilterContext = createContext(null);

export function FilterProvider({ children }) {
  const [filters, setFilters] = useState({
    sportType: "",
    athleteId: "",
    dateFrom: "",
    dateTo: "",
    distanceMin: "",
    distanceMax: "",
  });

  function updateFilter(key, value) {
    setFilters((prev) => ({ ...prev, [key]: value }));
  }

  function clearFilters() {
    setFilters({ sportType: "", athleteId: "", dateFrom: "", dateTo: "", distanceMin: "", distanceMax: "" });
  }

  return (
    <FilterContext.Provider value={{ filters, updateFilter, clearFilters }}>
      {children}
    </FilterContext.Provider>
  );
}

export function useFilters() {
  const ctx = useContext(FilterContext);
  if (!ctx) throw new Error("useFilters must be used within FilterProvider");
  return ctx;
}
