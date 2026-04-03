//! Dyops `dyops_core`: state-space observer for mean-reverting basis (OU) between physical and token prices.
//!
//! State `x = [basis, velocity, mean_level]`. The transition integrates mean-reversion speed `theta`
//! via an exact discrete-time map for the critically damped second-order OU structure.

mod observer;

pub use observer::{BasisObserver, ObserverInit, SystemHealth, WindowStats};

use numpy::{PyArray1, PyReadonlyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

#[pymodule]
fn dyops_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyBasisObserver>()?;
    m.add_class::<PySystemHealth>()?;
    m.add_class::<PyWindowStats>()?;
    Ok(())
}

/// Python-facing observer (wraps [`BasisObserver`]).
#[pyclass(name = "BasisObserver")]
struct PyBasisObserver {
    inner: BasisObserver,
}

#[pymethods]
impl PyBasisObserver {
    #[new]
    #[pyo3(signature = (name, theta, process_noise=None, measurement_noise=None, ring_buffer_capacity=None))]
    fn new(
        name: String,
        theta: f64,
        process_noise: Option<[f64; 9]>,
        measurement_noise: Option<f64>,
        ring_buffer_capacity: Option<usize>,
    ) -> PyResult<Self> {
        let init = ObserverInit {
            name,
            theta,
            q_process: process_noise,
            r_measurement: measurement_noise,
            ring_buffer_capacity,
        };
        let inner = BasisObserver::new(init).map_err(|e| PyErr::from(PyObserverError(e)))?;
        Ok(Self { inner })
    }

    fn update(&mut self, timestamp: f64, physical_price: f64, token_price: f64) -> PySystemHealth {
        let h = self.inner.update(timestamp, physical_price, token_price);
        PySystemHealth::from(h)
    }

    /// High-throughput batch path: one GIL hop and tight Rust loop over `float64` data.
    ///
    /// Arrays should be **one-dimensional**, **C-contiguous**, and **`float64`** (use
    /// `numpy.ascontiguousarray(..., dtype=numpy.float64)` when needed).
    #[pyo3(signature = (timestamps, physical, token))]
    fn update_batch<'py>(
        &mut self,
        py: Python<'py>,
        timestamps: PyReadonlyArray1<'py, f64>,
        physical: PyReadonlyArray1<'py, f64>,
        token: PyReadonlyArray1<'py, f64>,
    ) -> PyResult<Py<PyDict>> {
        let t = timestamps
            .as_slice()
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let p = physical
            .as_slice()
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let tok = token
            .as_slice()
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let n = t.len();
        if p.len() != n || tok.len() != n {
            return Err(PyValueError::new_err(
                "timestamps, physical, and token arrays must have the same length",
            ));
        }
        let (fb, inn, m) = self.inner.update_batch_slice(t, p, tok);

        let dict = PyDict::new(py);
        dict.set_item("filtered_basis", PyArray1::from_vec(py, fb))?;
        dict.set_item("innovation", PyArray1::from_vec(py, inn))?;
        dict.set_item("mahalanobis_distance", PyArray1::from_vec(py, m))?;
        Ok(dict.unbind())
    }

    /// Mean, variance, and Fisher excess kurtosis of innovations in the ring buffer.
    fn get_window_stats(&self) -> PyWindowStats {
        PyWindowStats::from(self.inner.window_stats())
    }

    /// Percentage of ring-buffer samples with Mahalanobis distance &gt; 3.0.
    fn get_criticality_score(&self) -> f64 {
        self.inner.criticality_score()
    }

    /// Criticality (`%` with Mahalanobis &gt; 3) over only the last `window` samples.
    fn get_criticality_recent(&self, window: usize) -> f64 {
        self.inner.criticality_recent(window)
    }

    /// Up to `n` most recent innovations from the ring (oldest → newest).
    fn get_last_innovations(&self, n: usize) -> Vec<f64> {
        self.inner.last_innovations(n)
    }

    /// `(filtered_basis, velocity)` from the internal state vector.
    fn get_basis_velocity(&self) -> (f64, f64) {
        self.inner.basis_velocity()
    }

    #[getter]
    fn name(&self) -> String {
        self.inner.name().to_string()
    }
}

#[derive(Clone)]
#[pyclass(name = "SystemHealth")]
struct PySystemHealth {
    #[pyo3(get)]
    filtered_basis: f64,
    #[pyo3(get)]
    innovation: f64,
    #[pyo3(get)]
    mahalanobis_distance: f64,
    #[pyo3(get)]
    measurement_valid: bool,
}

impl From<SystemHealth> for PySystemHealth {
    fn from(h: SystemHealth) -> Self {
        Self {
            filtered_basis: h.filtered_basis,
            innovation: h.innovation,
            mahalanobis_distance: h.mahalanobis_distance,
            measurement_valid: h.measurement_valid,
        }
    }
}

#[derive(Clone)]
#[pyclass(name = "WindowStats")]
struct PyWindowStats {
    #[pyo3(get)]
    mean: f64,
    #[pyo3(get)]
    variance: f64,
    #[pyo3(get)]
    kurtosis: f64,
}

impl From<WindowStats> for PyWindowStats {
    fn from(s: WindowStats) -> Self {
        Self {
            mean: s.mean,
            variance: s.variance,
            kurtosis: s.kurtosis,
        }
    }
}

struct PyObserverError(String);

impl From<PyObserverError> for PyErr {
    fn from(e: PyObserverError) -> Self {
        PyValueError::new_err(e.0)
    }
}
