//! Authentication and session utility functions.

use {
    crate::error::StakePoolError,
    fogo_sessions_sdk::session::{is_session, Session},
    solana_program::{
        account_info::AccountInfo, entrypoint::ProgramResult, program_error::ProgramError,
        pubkey::Pubkey,
    },
};

/// Extract user public key from either direct signer or session account
pub(crate) fn extract_user_from_signer_or_session(
    account_info: &AccountInfo,
    program_id: &Pubkey,
) -> Result<Pubkey, ProgramError> {
    if is_session(account_info) {
        Session::extract_user_from_signer_or_session(account_info, program_id)
            .map_err(|_| StakePoolError::SessionValidationFailed.into())
    } else {
        if !account_info.is_signer {
            return Err(StakePoolError::SignatureMissing.into());
        }
        Ok(*account_info.key)
    }
}

/// Validate session context if needed
pub(crate) fn validate_session_context(
    account_info: &AccountInfo,
    program_id: &Pubkey,
) -> ProgramResult {
    if is_session(account_info) {
        // Additional session validation can be added here if needed
        Session::extract_user_from_signer_or_session(account_info, program_id)
            .map_err(|_| ProgramError::from(StakePoolError::InvalidSession))?;
    }
    Ok(())
}
