//! Thermal: compose print jobs from a handful of primitives and send them to an
//! ESC/POS printer, from the command line or over HTTP.
//!
//! The pipeline is the same for both front ends:
//!
//!   JSON job/template --job--> Job --layout--> Layout --escpos--> bytes --transport--> printer
//!                                                    \--preview--> text
//!
//! Layout is the single place where paper geometry is applied, so the bytes
//! and the preview cannot disagree about where a line wraps.

pub mod config;
pub mod error;
pub mod escpos;
pub mod job;
pub mod layout;
pub mod transport;
pub mod ops;
pub mod preview;
pub mod server;
pub mod text;

/// The OpenAPI document, compiled in so the binary can serve it anywhere.
pub const OPENAPI_YAML: &str = include_str!("../plugin/thermal/openapi.yaml");
