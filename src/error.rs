use std::fmt;

/// One error type for CLI and HTTP: a message, an optional hint on what to do
/// about it, and the HTTP status it maps to.
#[derive(Debug, Clone)]
pub struct Error {
    pub status: u16,
    pub message: String,
    pub hint: Option<String>,
}

pub type Result<T> = std::result::Result<T, Error>;

impl Error {
    pub fn new(status: u16, message: impl Into<String>) -> Self {
        Error { status, message: message.into(), hint: None }
    }

    /// A problem with the job itself.
    pub fn bad(message: impl Into<String>) -> Self {
        Error::new(400, message)
    }

    pub fn hint(mut self, hint: impl Into<String>) -> Self {
        self.hint = Some(hint.into());
        self
    }

    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({ "error": self.message, "hint": self.hint })
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.message)?;
        if let Some(hint) = &self.hint {
            write!(f, "\n  hint: {hint}")?;
        }
        Ok(())
    }
}

impl From<serde_json::Error> for Error {
    fn from(e: serde_json::Error) -> Self {
        Error::bad(format!("invalid job: {e}"))
    }
}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::new(500, e.to_string())
    }
}
