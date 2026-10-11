//! model_rs: JSONL front-end for the independent ISA model (INTERFACE.md).

mod json;
mod model;

use json::Json;
use std::fmt::Write as _;
use std::io::{self, BufRead, BufWriter, Write};

fn byte_array(v: Option<&Json>, name: &str, required: bool, exact_len: Option<usize>) -> Result<Vec<u8>, String> {
    let v = match v {
        None if !required => return Ok(Vec::new()),
        None => return Err(format!("missing field '{}'", name)),
        Some(v) => v,
    };
    let arr = match v {
        Json::Arr(a) => a,
        _ => return Err(format!("field '{}' must be an array", name)),
    };
    if let Some(n) = exact_len {
        if arr.len() != n {
            return Err(format!("field '{}' must have {} entries, got {}", name, n, arr.len()));
        }
    }
    arr.iter()
        .enumerate()
        .map(|(i, x)| match x {
            Json::Int(n) if (0..=255).contains(n) => Ok(*n as u8),
            _ => Err(format!("{}[{}] must be an int 0-255", name, i)),
        })
        .collect()
}

fn write_ints<T: std::fmt::Display>(out: &mut String, xs: &[T]) {
    out.push('[');
    for (i, x) in xs.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        let _ = write!(out, "{}", x);
    }
    out.push(']');
}

fn write_state_fields(out: &mut String, s: &model::State) {
    let _ = write!(out, "\"pc\":{},\"regs\":", s.pc);
    write_ints(out, &s.regs);
    let _ = write!(
        out,
        ",\"flag\":{},\"retaddr\":{},\"return_valid\":{},\"illegal_op_flag\":{},\"call_ret_misuse_flag\":{},\"halted\":{},\"uo_out\":{},\"pin_mode\":",
        s.flag, s.retaddr, s.return_valid, s.illegal_op_flag, s.call_ret_misuse_flag, s.halted, s.uo_out
    );
    write_ints(out, &s.pin_mode);
    out.push_str(",\"pin_drv\":");
    write_ints(out, &s.pin_drv);
}

fn handle(line: &str) -> String {
    let parsed = json::parse(line);
    let id = match &parsed {
        Ok(v) => v.get("id").cloned().unwrap_or(Json::Null),
        Err(_) => Json::Null,
    };
    let mut out = String::new();
    let result = (|| -> Result<model::RunResult, String> {
        let v = parsed.clone()?;
        if !matches!(v, Json::Obj(_)) {
            return Err("input line must be a JSON object".into());
        }
        let image = byte_array(v.get("image"), "image", true, Some(model::MEM_BYTES))?;
        let ui = byte_array(v.get("ui"), "ui", false, None)?;
        let uio = byte_array(v.get("uio"), "uio", false, None)?;
        let max_cycles = match v.get("max_cycles") {
            Some(Json::Int(n)) if *n >= 0 && *n <= u64::MAX as i128 / 2 => *n as u64,
            Some(_) => return Err("max_cycles must be a non-negative int".into()),
            None => return Err("missing field 'max_cycles'".into()),
        };
        Ok(model::run(&image, &ui, &uio, max_cycles))
    })();
    out.push_str("{\"id\":");
    id.write(&mut out);
    match result {
        Err(e) => {
            out.push_str(",\"error\":");
            json::write_str(&mut out, &e);
            out.push('}');
        }
        Ok(r) => {
            out.push_str(",\"commits\":[");
            for (i, c) in r.commits.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                let _ = write!(out, "{{\"pc\":{},\"word\":{},\"cycles\":{},\"state\":{{", c.pc, c.word, c.cycles);
                write_state_fields(&mut out, &c.state);
                out.push_str("}}");
            }
            out.push_str("],\"final\":{");
            write_state_fields(&mut out, &r.final_state);
            out.push_str(",\"data_mem\":");
            write_ints(&mut out, &r.data_mem);
            let _ = write!(out, "}},\"halted\":{},\"timeout\":{}}}", r.halted, r.timeout);
        }
    }
    out
}

fn main() {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut w = BufWriter::new(stdout.lock());
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(e) => {
                let _ = writeln!(w, "{{\"id\":null,\"error\":\"read error: {}\"}}", e.to_string().replace('"', "'"));
                continue;
            }
        };
        if line.trim().is_empty() {
            continue;
        }
        let o = handle(&line);
        if writeln!(w, "{}", o).is_err() {
            break;
        }
    }
    let _ = w.flush();
}

#[cfg(test)]
mod tests {
    use super::*;

    // INTERFACE.md: malformed input line -> {"id":...,"error":...}, no crash
    #[test]
    fn malformed_lines_answered_with_error() {
        let o = handle("not json");
        assert!(o.starts_with("{\"id\":null,\"error\":"));
        let o = handle(r#"{"id":"x","image":[1,2],"max_cycles":10}"#);
        assert!(o.starts_with("{\"id\":\"x\",\"error\":"));
        let o = handle(r#"{"id":7,"image":[],"max_cycles":-1}"#);
        assert!(o.starts_with("{\"id\":7,\"error\":"));
    }

    // INTERFACE.md output shape
    #[test]
    fn output_shape() {
        let img: Vec<String> = model::image_of(&[model::enc_op(model::OP_HALT)]).iter().map(|b| b.to_string()).collect();
        let line = format!(r#"{{"id":"s1","image":[{}],"ui":[],"uio":[],"max_cycles":100}}"#, img.join(","));
        let o = handle(&line);
        let v = json::parse(&o).unwrap();
        assert_eq!(v.get("halted"), Some(&Json::Bool(true)));
        assert_eq!(v.get("timeout"), Some(&Json::Bool(false)));
        let fin = v.get("final").unwrap();
        match fin.get("data_mem") {
            Some(Json::Arr(a)) => assert_eq!(a.len(), 512),
            _ => panic!(),
        }
        match v.get("commits") {
            Some(Json::Arr(a)) => {
                assert_eq!(a.len(), 1);
                assert_eq!(a[0].get("word"), Some(&Json::Int(2048)));
                assert_eq!(a[0].get("cycles"), Some(&Json::Int(3)));
            }
            _ => panic!(),
        }
    }
}
